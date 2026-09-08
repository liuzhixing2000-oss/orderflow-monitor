"""Prospective collection only: no parameter search or early performance verdict."""
from __future__ import annotations

import hashlib
from importlib.metadata import version
import json
import math
import os
import time
from pathlib import Path

try:
    from .config import settings
except ImportError:
    from config import settings

COHORT = "sample-b-v01"
MODEL = "0.1.2-research"
WARMUP = 14_400
HORIZON = 14_400
GAP = 90


def manifest():
    root = Path(__file__).parent
    files = ("sample_b.py", "SAMPLE_B_PROTOCOL.md", "engine.py", "research_models.py",
             "storage.py", "historical_context.py", "main.py", "config.py", "requirements.txt")
    required = {"round_trip_cost_pct": .12, "snapshot_interval_seconds": 60,
                "research_price_path_interval_seconds": 10, "book_depth": 200,
                "research_min_1h_span_seconds": 3420, "research_min_4h_span_seconds": 13680,
                "research_min_1h_samples": 45, "research_min_4h_samples": 180}
    for key, value in required.items():
        if getattr(settings, key) != value:
            raise ValueError("Sample B frozen configuration mismatch: " + key)
    config = settings.model_dump(exclude={"api_key", "data_path", "sample_b_enabled"})
    value = {"cohort": COHORT, "model": MODEL, "files": {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files},
        "settings": config, "runtime_packages": {name: version(name) for name in
            ("pydantic", "pydantic-settings", "fastapi", "mcp", "websockets", "httpx")}}
    value["fingerprint"] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return value


class SampleB:
    def __init__(self, storage):
        self.storage = storage
        self.session_start = time.time()
        self.blocked = None
        self.started = False

    def init(self, now=None, provenance=None):
        now = time.time() if now is None else now
        self.session_start = now
        if provenance is None:
            try:
                provenance = manifest()
            except ValueError as exc:
                self.blocked = str(exc)
                return
        with self.storage.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sample_b_cohort (
                    id TEXT PRIMARY KEY, started_ts REAL NOT NULL,
                    fingerprint TEXT NOT NULL, manifest TEXT NOT NULL, source_commit TEXT,
                    blocked_reason TEXT);
                CREATE TABLE IF NOT EXISTS sample_b_observations (
                    id INTEGER PRIMARY KEY, cohort TEXT NOT NULL, ts REAL NOT NULL,
                    price REAL, score REAL, eligible INTEGER NOT NULL, price_gate INTEGER NOT NULL,
                    primary_event INTEGER NOT NULL, price_event INTEGER NOT NULL,
                    reason TEXT NOT NULL, payload TEXT NOT NULL, outcome TEXT,
                    UNIQUE(cohort, ts));
                CREATE INDEX IF NOT EXISTS idx_sample_b_pending
                    ON sample_b_observations(cohort, ts)
                    WHERE outcome IS NULL AND (price_gate=1 OR primary_event=1);
                CREATE INDEX IF NOT EXISTS idx_sample_b_primary
                    ON sample_b_observations(cohort, ts) WHERE primary_event=1;
                CREATE INDEX IF NOT EXISTS idx_sample_b_price
                    ON sample_b_observations(cohort, ts) WHERE price_event=1;
            """)
            row = db.execute("SELECT * FROM sample_b_cohort WHERE id=?", (COHORT,)).fetchone()
            if row is None:
                db.execute("INSERT INTO sample_b_cohort VALUES(?,?,?,?,?,NULL)",
                           (COHORT, now, provenance["fingerprint"], json.dumps(provenance, sort_keys=True),
                            os.getenv("RAILWAY_GIT_COMMIT_SHA")))
            elif row["fingerprint"] != provenance["fingerprint"]:
                db.execute("UPDATE sample_b_cohort SET blocked_reason='VERSION_DRIFT' WHERE id=?", (COHORT,))
                self.blocked = "VERSION_DRIFT"
            else:
                self.blocked = row["blocked_reason"]
        self.started = True

    def observe(self, snapshot):
        if not self.started or self.blocked or snapshot.get("symbol") != "ETHUSDT":
            return
        ts = float(snapshot["timestamp"])
        models = snapshot.get("models") or {}
        quality = models.get("data_quality") or {}
        price = snapshot.get("price")
        score = (models.get("continuation") or {}).get("long")
        stats = snapshot.get("price_stats") or {}
        r1 = (stats.get("1h") or {}).get("return_pct")
        r4 = (stats.get("4h") or {}).get("return_pct")
        age = snapshot.get("last_update_age_seconds")
        reasons = []
        if models.get("version") != MODEL:
            self.blocked = "MODEL_VERSION_DRIFT"
            with self.storage.connect() as db:
                db.execute("UPDATE sample_b_cohort SET blocked_reason=? WHERE id=?", (self.blocked, COHORT))
            return
        if ts < self.session_start + WARMUP:
            reasons.append("FRESH_SESSION_WARMUP")
        if quality.get("state") != "OK" or not quality.get("eligible", True):
            reasons.append("DATA_QUALITY:" + str(quality.get("state")))
        if age is None or not 0 <= age <= 5:
            reasons.append("STALE_PRICE")
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            reasons.append("MISSING_PRICE")
        if not isinstance(score, (int, float)) or not math.isfinite(score):
            reasons.append("MISSING_SCORE")
        eligible = not reasons
        price_gate = bool(eligible and r1 is not None and r4 is not None and r1 >= .05 and r4 >= .15)
        signal_eligible = eligible and (models.get("regime") or {}).get("label") == "TREND_UP"
        with self.storage.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM sample_b_observations WHERE cohort=? ORDER BY ts DESC LIMIT 1", (COHORT,)).fetchone()
            if previous and ts <= previous["ts"]:
                return  # Idempotent retry and out-of-order rejection; never rewrite history.
            adjacent = bool(previous and 0 < ts - previous["ts"] <= GAP)
            prev_signal = bool(previous and json.loads(previous["payload"])["signal_eligible"])
            crossing = bool(signal_eligible and adjacent and prev_signal and previous["score"] < 70 <= score)
            last_signal = db.execute("SELECT MAX(ts) FROM sample_b_observations WHERE cohort=? AND primary_event=1", (COHORT,)).fetchone()[0]
            primary = crossing and (last_signal is None or ts - last_signal >= HORIZON)
            last_price = db.execute("SELECT MAX(ts) FROM sample_b_observations WHERE cohort=? AND price_event=1", (COHORT,)).fetchone()[0]
            # Same fixed A comparator: first price-eligible observation, then first
            # eligible observation after each four-hour cooldown (not a score crossing).
            benchmark = price_gate and (last_price is None or ts - last_price >= HORIZON)
            if eligible:
                if not signal_eligible:
                    reasons.append("REGIME_NOT_TREND_UP")
                elif not adjacent or not prev_signal:
                    reasons.append("NO_ADJACENT_ELIGIBLE_PREDECESSOR")
                elif not crossing:
                    reasons.append("NO_THRESHOLD_CROSSING")
                elif not primary:
                    reasons.append("COOLDOWN")
                else:
                    reasons.append("PRIMARY_EVENT")
            path = db.execute("SELECT price FROM price_path WHERE symbol='ETHUSDT' AND ts>=? AND ts<? ORDER BY ts", (ts-3600, ts)).fetchall()
            prices = [float(p[0]) for p in path] + ([price] if price and price > 0 else [])
            rv = math.sqrt(sum(math.log(b/a)**2 for a,b in zip(prices, prices[1:]))) * 100 if len(prices) >= 2 else None
            payload = {"snapshot": snapshot, "signal_eligible": bool(signal_eligible),
                       "rv_1h_pct": rv, "session_start": self.session_start,
                       "snapshot_sha256": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()}
            db.execute("INSERT INTO sample_b_observations(cohort,ts,price,score,eligible,price_gate,primary_event,price_event,reason,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (COHORT, ts, price, score, int(eligible), int(price_gate), int(primary), int(benchmark), ";".join(reasons), json.dumps(payload, separators=(",", ":"))))

    def evaluate(self, now=None, limit=100):
        if not self.started or self.blocked:
            return 0
        now = time.time() if now is None else now
        with self.storage.connect() as db:
            rows = db.execute("SELECT * FROM sample_b_observations WHERE cohort=? AND outcome IS NULL AND (price_gate=1 OR primary_event=1) AND ts<=? ORDER BY ts LIMIT ?", (COHORT, now-HORIZON-180, limit)).fetchall()
            for row in rows:
                event = {"id": row["id"], "ts": row["ts"], "symbol": "ETHUSDT", "side": "long", "price": row["price"]}
                outcome = self.storage._evaluate_event_path(db, event, 240)
                # Also retain synchronized BTC prices for later fixed beta attribution.
                outcome["btc_entry"] = self._btc_price(db, row["ts"])
                outcome["btc_exit"] = self._btc_price(db, outcome["exit_ts"]) if outcome["exit_ts"] else None
                db.execute("UPDATE sample_b_observations SET outcome=? WHERE id=? AND outcome IS NULL", (json.dumps(outcome, separators=(",", ":")), row["id"]))
        return len(rows)

    @staticmethod
    def _btc_price(db, ts):
        row = db.execute("SELECT ts,price FROM price_path WHERE symbol='BTCUSDT' AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1", (ts, ts-90)).fetchone()
        return dict(row) if row else None

    def status(self, now=None):
        if not self.started:
            return {"enabled": False, "state": self.blocked or "NOT_STARTED"}
        now = time.time() if now is None else now
        with self.storage.connect() as db:
            cohort = dict(db.execute("SELECT * FROM sample_b_cohort WHERE id=?", (COHORT,)).fetchone())
            rows = db.execute("SELECT primary_event,outcome,COUNT(*) n FROM sample_b_observations WHERE cohort=? GROUP BY primary_event,outcome IS NULL,CASE WHEN outcome IS NOT NULL THEN json_extract(outcome,'$.quality_ok') END", (COHORT,)).fetchall()
            counts = {"observations": 0, "primary_events": 0, "primary_mature_quality_ok": 0, "primary_mature_excluded": 0}
            for row in rows:
                counts["observations"] += row["n"]
                if row["primary_event"]:
                    counts["primary_events"] += row["n"]
                    if row["outcome"]:
                        counts["primary_mature_quality_ok" if json.loads(row["outcome"])["quality_ok"] else "primary_mature_excluded"] += row["n"]
            bounds = db.execute("SELECT MIN(ts),MAX(ts) FROM sample_b_observations WHERE cohort=? AND eligible=1", (COHORT,)).fetchone()
            reasons = {r[0]: r[1] for r in db.execute("SELECT reason,COUNT(*) FROM sample_b_observations WHERE cohort=? GROUP BY reason", (COHORT,))}
        # Count complete UTC days after first eligibility, through actual observations;
        # a stopped collector does not accrue days merely because wall time passes.
        days = max(0, int(bounds[1]//86400) - math.ceil(bounds[0]/86400)) if bounds[0] is not None else 0
        ready = days >= 30 and counts["primary_mature_quality_ok"] >= 100 and not self.blocked
        return {"enabled": True, "state": self.blocked or ("AWAITING_PRESPECIFIED_ANALYSIS" if ready else "COLLECTING"),
                "cohort": COHORT, "started_ts": cohort["started_ts"], "fingerprint": cohort["fingerprint"],
                "source_commit": cohort["source_commit"], "session_warmup_until": self.session_start + WARMUP,
                **counts, "complete_utc_days_elapsed": days, "exclusion_counts": reasons,
                "minimum_evidence_gate_met": ready, "edge_verdict": "NOT_ESTABLISHED"}

    def export(self, after_id=0, limit=500):
        if not self.started:
            return {"enabled": False, "rows": []}
        with self.storage.connect() as db:
            cohort = dict(db.execute("SELECT * FROM sample_b_cohort WHERE id=?", (COHORT,)).fetchone())
            rows = [dict(row) for row in db.execute("SELECT * FROM sample_b_observations WHERE cohort=? AND id>? ORDER BY id LIMIT ?", (COHORT, after_id, limit))]
        return {"cohort": cohort, "rows": rows, "next_after_id": rows[-1]["id"] if rows else after_id,
                "note": "Re-export from zero after outcomes mature; earlier rows gain outcomes. No efficacy inference before evidence gates."}
