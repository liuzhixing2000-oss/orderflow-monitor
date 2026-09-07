from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

try:
    from .config import settings
except ImportError:  # Flat GitHub upload compatibility.
    from config import settings


VALID_HORIZONS = (15, 30, 60, 240)


class Storage:
    def __init__(self, path: str | None = None) -> None:
        wanted = path or settings.data_path
        try:
            Path(wanted).parent.mkdir(parents=True, exist_ok=True)
            self.path = wanted
        except PermissionError:
            self.path = "orderflow.db"

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def init(self) -> None:
        with self.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    symbol TEXT NOT NULL,
                    price REAL NOT NULL,
                    long_score INTEGER NOT NULL,
                    short_score INTEGER NOT NULL,
                    stance TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_ts ON snapshots(symbol, ts)")

            db.execute("""
                CREATE TABLE IF NOT EXISTS research_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    symbol TEXT NOT NULL,
                    model TEXT NOT NULL,
                    side TEXT NOT NULL,
                    threshold INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    price REAL NOT NULL,
                    regime TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(symbol, model, side, threshold, ts)
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_research_events_lookup "
                "ON research_events(symbol, model, side, threshold, ts)"
            )

            db.execute("""
                CREATE TABLE IF NOT EXISTS research_event_state (
                    symbol TEXT NOT NULL,
                    model TEXT NOT NULL,
                    side TEXT NOT NULL,
                    threshold INTEGER NOT NULL,
                    last_score REAL,
                    last_eligible INTEGER NOT NULL DEFAULT 0,
                    last_event_ts REAL,
                    updated_ts REAL NOT NULL,
                    PRIMARY KEY(symbol, model, side, threshold)
                )
            """)

            # Persist a lightweight 10-second price path so a safe redeploy does
            # not erase the entire 4h warmup history.
            db.execute("""
                CREATE TABLE IF NOT EXISTS price_path (
                    ts REAL NOT NULL,
                    symbol TEXT NOT NULL,
                    price REAL NOT NULL,
                    PRIMARY KEY(symbol, ts)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_price_path_ts ON price_path(ts)")

    def insert(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        if not snapshot.get("price"):
            return []
        score = snapshot["assessment"]
        with self.connect() as db:
            db.execute(
                "INSERT INTO snapshots(ts,symbol,price,long_score,short_score,stance,payload) VALUES(?,?,?,?,?,?,?)",
                (snapshot["timestamp"], snapshot["symbol"], snapshot["price"], score["long_score"],
                 score["short_score"], score["stance"], json.dumps(snapshot, separators=(",", ":"))),
            )
            events = self._process_research_events(db, snapshot)
        return events

    def _process_research_events(self, db: sqlite3.Connection,
                                 snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        models = snapshot.get("models") or {}
        continuation = models.get("continuation") or {}
        quality = models.get("data_quality") or {}
        regime = models.get("regime") or {}
        if not continuation:
            return []

        ts = float(snapshot["timestamp"])
        symbol = str(snapshot["symbol"])
        price = float(snapshot["price"])
        regime_label = regime.get("label")
        model_version = str(models.get("version") or "unknown")
        quality_ok = quality.get("state") == "OK" and bool(quality.get("eligible", True))
        cooldown_seconds = settings.research_event_cooldown_minutes * 60
        created: list[dict[str, Any]] = []

        for side, wanted_regime in (("long", "TREND_UP"), ("short", "TREND_DOWN")):
            raw_score = continuation.get(side)
            if raw_score is None:
                continue
            current_score = int(raw_score)
            eligible = quality_ok and regime_label == wanted_regime

            for threshold in settings.research_threshold_list:
                row = db.execute(
                    """
                    SELECT last_score,last_eligible,last_event_ts
                    FROM research_event_state
                    WHERE symbol=? AND model='continuation' AND side=? AND threshold=?
                    """,
                    (symbol, side, threshold),
                ).fetchone()

                previous_score = row["last_score"] if row else None
                previous_eligible = bool(row["last_eligible"]) if row else False
                previous_event_ts = row["last_event_ts"] if row else None
                crossing = (
                    eligible
                    and previous_eligible
                    and previous_score is not None
                    and float(previous_score) < threshold <= current_score
                )
                cooldown_ok = (
                    previous_event_ts is None
                    or ts - float(previous_event_ts) >= cooldown_seconds
                )
                new_event_ts = previous_event_ts

                if crossing and cooldown_ok:
                    event_payload = {
                        "data_quality": quality,
                        "regime": regime,
                        "continuation": continuation,
                    }
                    db.execute(
                        """
                        INSERT OR IGNORE INTO research_events(
                            ts,symbol,model,side,threshold,score,price,regime,model_version,payload
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            ts, symbol, "continuation", side, threshold, current_score, price,
                            regime_label, model_version,
                            json.dumps(event_payload, separators=(",", ":")),
                        ),
                    )
                    new_event_ts = ts
                    created.append({
                        "ts": ts,
                        "symbol": symbol,
                        "model": "continuation",
                        "side": side,
                        "threshold": threshold,
                        "score": current_score,
                        "price": price,
                        "regime": regime_label,
                    })

                db.execute(
                    """
                    INSERT INTO research_event_state(
                        symbol,model,side,threshold,last_score,last_eligible,last_event_ts,updated_ts
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(symbol,model,side,threshold) DO UPDATE SET
                        last_score=excluded.last_score,
                        last_eligible=excluded.last_eligible,
                        last_event_ts=excluded.last_event_ts,
                        updated_ts=excluded.updated_ts
                    """,
                    (
                        symbol, "continuation", side, threshold, current_score,
                        1 if eligible else 0, new_event_ts, ts,
                    ),
                )
        return created

    def insert_price_points(self, points: list[tuple[float, str, float]]) -> None:
        if not points:
            return
        cutoff = time.time() - settings.history_seconds
        with self.connect() as db:
            db.executemany(
                "INSERT OR REPLACE INTO price_path(ts,symbol,price) VALUES(?,?,?)",
                points,
            )
            db.execute("DELETE FROM price_path WHERE ts<?", (cutoff,))

    def load_price_history(self, lookback_seconds: int = 14_400) -> dict[str, list[tuple[float, float]]]:
        cutoff = time.time() - lookback_seconds
        with self.connect() as db:
            rows = db.execute(
                "SELECT ts,symbol,price FROM price_path WHERE ts>=? ORDER BY symbol,ts",
                (cutoff,),
            ).fetchall()
        out: dict[str, list[tuple[float, float]]] = {}
        for row in rows:
            out.setdefault(row["symbol"], []).append((float(row["ts"]), float(row["price"])))
        return out

    def status(self) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT symbol,COUNT(*) n,MIN(ts) first_ts,MAX(ts) last_ts FROM snapshots GROUP BY symbol"
            ).fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    def event_status(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT symbol,model,side,threshold,COUNT(*) n,MIN(ts) first_ts,MAX(ts) last_ts
                FROM research_events
                GROUP BY symbol,model,side,threshold
                ORDER BY symbol,model,side,threshold
            """).fetchall()
        return [dict(r) for r in rows]

    def buckets(self, symbol: str, horizon_minutes: int, side: str) -> list[dict[str, Any]]:
        if horizon_minutes not in VALID_HORIZONS:
            raise ValueError("horizon must be 15, 30, 60 or 240")
        if side not in ("long", "short"):
            raise ValueError("side must be long or short")
        target = horizon_minutes * 60
        score_col = "long_score" if side == "long" else "short_score"
        direction = 1 if side == "long" else -1
        # Legacy minute-by-minute research. These observations are correlated;
        # independent threshold events are preferred for formal validation.
        sql = f"""
            SELECT a.{score_col} score, a.price entry_price,
                   (SELECT b.price FROM snapshots b
                    WHERE b.symbol=a.symbol AND b.ts>=a.ts+?
                    ORDER BY b.ts LIMIT 1) exit_price
            FROM snapshots a
            WHERE a.symbol=? AND a.ts<=?
        """
        now = time.time()
        with self.connect() as db:
            rows = db.execute(sql, (target, symbol, now-target)).fetchall()
        groups: dict[str, list[float]] = {
            "0-59": [], "60-69": [], "70-79": [], "80-89": [], "90-100": []
        }
        for r in rows:
            if r["exit_price"] is None:
                continue
            score = r["score"]
            label = self._score_band(score)
            gross = direction * (r["exit_price"] / r["entry_price"] - 1) * 100
            groups[label].append(gross - settings.round_trip_cost_pct)
        result = []
        for label, values in groups.items():
            wins = [v for v in values if v > 0]
            losses = [v for v in values if v <= 0]
            pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) else None
            result.append({
                "score_band": label,
                "samples": len(values),
                "win_rate": round(len(wins)/len(values), 4) if values else None,
                "avg_net_return_pct": round(sum(values)/len(values), 5) if values else None,
                "profit_factor": round(pf, 3) if pf is not None else None,
            })
        return result

    def event_results(self, symbol: str, horizon_minutes: int, side: str,
                      threshold: int, model: str = "continuation") -> dict[str, Any]:
        if horizon_minutes not in VALID_HORIZONS:
            raise ValueError("horizon must be 15, 30, 60 or 240")
        if side not in ("long", "short"):
            raise ValueError("side must be long or short")
        if threshold not in settings.research_threshold_list:
            raise ValueError(f"threshold must be one of {settings.research_threshold_list}")
        if model != "continuation":
            raise ValueError("only continuation is implemented in v0.1")

        target = horizon_minutes * 60
        tolerance = max(180, settings.snapshot_interval_seconds * 3)
        direction = 1 if side == "long" else -1
        cutoff = time.time() - target
        sql = """
            SELECT e.id,e.ts,e.score,e.price entry_price,e.regime,e.model_version,
                   (SELECT b.price FROM snapshots b
                    WHERE b.symbol=e.symbol
                      AND b.ts>=e.ts+?
                      AND b.ts<=e.ts+?+?
                    ORDER BY b.ts LIMIT 1) exit_price,
                   (SELECT MIN(b.price) FROM snapshots b
                    WHERE b.symbol=e.symbol AND b.ts>=e.ts AND b.ts<=e.ts+?) min_price,
                   (SELECT MAX(b.price) FROM snapshots b
                    WHERE b.symbol=e.symbol AND b.ts>=e.ts AND b.ts<=e.ts+?) max_price
            FROM research_events e
            WHERE e.symbol=? AND e.model=? AND e.side=? AND e.threshold=? AND e.ts<=?
            ORDER BY e.ts
        """
        with self.connect() as db:
            rows = db.execute(
                sql,
                (target, target, tolerance, target, target,
                 symbol, model, side, threshold, cutoff),
            ).fetchall()
            total_events = db.execute(
                "SELECT COUNT(*) n FROM research_events WHERE symbol=? AND model=? AND side=? AND threshold=?",
                (symbol, model, side, threshold),
            ).fetchone()["n"]

        observations: list[dict[str, Any]] = []
        for row in rows:
            if row["exit_price"] is None or row["min_price"] is None or row["max_price"] is None:
                continue
            entry = float(row["entry_price"])
            exit_price = float(row["exit_price"])
            min_price = float(row["min_price"])
            max_price = float(row["max_price"])
            net = direction * (exit_price / entry - 1) * 100 - settings.round_trip_cost_pct
            directional_extremes = [
                direction * (min_price / entry - 1) * 100,
                direction * (max_price / entry - 1) * 100,
            ]
            observations.append({
                "id": row["id"],
                "ts": float(row["ts"]),
                "score": int(row["score"]),
                "regime": row["regime"],
                "model_version": row["model_version"],
                "net": net,
                "mfe": max(directional_extremes),
                "mae": min(directional_extremes),
            })

        values = [x["net"] for x in observations]
        mfes = [x["mfe"] for x in observations]
        maes = [x["mae"] for x in observations]
        wins = [v for v in values if v > 0]
        losses = [v for v in values if v <= 0]
        pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) else None

        weekly: dict[str, list[float]] = {}
        for obs in observations:
            week = datetime.fromtimestamp(obs["ts"], tz=timezone.utc).strftime("%G-W%V")
            weekly.setdefault(week, []).append(obs["net"])
        weekly_rows = []
        for week, week_values in sorted(weekly.items()):
            week_wins = [v for v in week_values if v > 0]
            weekly_rows.append({
                "week": week,
                "samples": len(week_values),
                "avg_net_return_pct": round(sum(week_values)/len(week_values), 5),
                "win_rate": round(len(week_wins)/len(week_values), 4),
            })

        return {
            "symbol": symbol,
            "model": model,
            "side": side,
            "threshold": threshold,
            "horizon_minutes": horizon_minutes,
            "cooldown_minutes": settings.research_event_cooldown_minutes,
            "round_trip_cost_pct": settings.round_trip_cost_pct,
            "events_recorded": int(total_events),
            "events_evaluated": len(values),
            "avg_net_return_pct": round(sum(values)/len(values), 5) if values else None,
            "median_net_return_pct": round(median(values), 5) if values else None,
            "win_rate": round(len(wins)/len(values), 4) if values else None,
            "profit_factor": round(pf, 3) if pf is not None else None,
            "avg_mfe_pct": round(sum(mfes)/len(mfes), 5) if mfes else None,
            "avg_mae_pct": round(sum(maes)/len(maes), 5) if maes else None,
            "weekly_stability": weekly_rows,
            "note": "Events are threshold crossings with per-threshold cooldown; different thresholds can still describe the same market episode.",
        }

    def event_threshold_sweep(self, symbol: str, horizon_minutes: int,
                              side: str, model: str = "continuation") -> list[dict[str, Any]]:
        rows = []
        for threshold in settings.research_threshold_list:
            result = self.event_results(symbol, horizon_minutes, side, threshold, model)
            rows.append({
                "threshold": threshold,
                "events_recorded": result["events_recorded"],
                "events_evaluated": result["events_evaluated"],
                "avg_net_return_pct": result["avg_net_return_pct"],
                "median_net_return_pct": result["median_net_return_pct"],
                "win_rate": result["win_rate"],
                "profit_factor": result["profit_factor"],
                "avg_mfe_pct": result["avg_mfe_pct"],
                "avg_mae_pct": result["avg_mae_pct"],
            })
        return rows

    @staticmethod
    def _score_band(score: int | float) -> str:
        return (
            "0-59" if score < 60 else
            "60-69" if score < 70 else
            "70-79" if score < 80 else
            "80-89" if score < 90 else
            "90-100"
        )


storage = Storage()
