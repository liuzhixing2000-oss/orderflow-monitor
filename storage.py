from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

try:
    from .config import settings
except ImportError:  # Flat GitHub upload compatibility.
    from config import settings


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

    def insert(self, snapshot: dict[str, Any]) -> None:
        if not snapshot.get("price"):
            return
        score = snapshot["assessment"]
        with self.connect() as db:
            db.execute(
                "INSERT INTO snapshots(ts,symbol,price,long_score,short_score,stance,payload) VALUES(?,?,?,?,?,?,?)",
                (snapshot["timestamp"], snapshot["symbol"], snapshot["price"], score["long_score"],
                 score["short_score"], score["stance"], json.dumps(snapshot, separators=(",", ":"))),
            )

    def status(self) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute("SELECT symbol,COUNT(*) n,MIN(ts) first_ts,MAX(ts) last_ts FROM snapshots GROUP BY symbol").fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    def buckets(self, symbol: str, horizon_minutes: int, side: str) -> list[dict[str, Any]]:
        if horizon_minutes not in (15, 30, 60, 240):
            raise ValueError("horizon must be 15, 30, 60 or 240")
        if side not in ("long", "short"):
            raise ValueError("side must be long or short")
        target = horizon_minutes * 60
        score_col = "long_score" if side == "long" else "short_score"
        direction = 1 if side == "long" else -1
        # Match each observation to the first recorded snapshot at/after its horizon.
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
        groups: dict[str, list[float]] = {"0-59": [], "60-69": [], "70-79": [], "80-89": [], "90-100": []}
        for r in rows:
            if r["exit_price"] is None: continue
            score = r["score"]
            label = "0-59" if score < 60 else "60-69" if score < 70 else "70-79" if score < 80 else "80-89" if score < 90 else "90-100"
            gross = direction * (r["exit_price"] / r["entry_price"] - 1) * 100
            groups[label].append(gross - settings.round_trip_cost_pct)
        result = []
        for label, values in groups.items():
            wins = [v for v in values if v > 0]; losses = [v for v in values if v <= 0]
            pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) else None
            result.append({"score_band": label, "samples": len(values),
                           "win_rate": round(len(wins)/len(values), 4) if values else None,
                           "avg_net_return_pct": round(sum(values)/len(values), 5) if values else None,
                           "profit_factor": round(pf, 3) if pf is not None else None})
        return result


storage = Storage()
