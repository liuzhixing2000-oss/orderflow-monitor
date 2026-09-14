from __future__ import annotations

import bisect
import json
import os
import sqlite3
from collections import defaultdict

from legacy_replay_backtest import (
    COOLDOWN_SECONDS,
    DB_PATH,
    HORIZONS_MIN,
    MIN_1H_SAMPLES,
    MIN_1H_SPAN,
    MIN_4H_SAMPLES,
    MIN_4H_SPAN,
    ROUND_TRIP_COST_PCT,
    THRESHOLDS,
    calc_price_stats,
    classify_regime,
    continuation_score,
    normalized_delta,
    summarize,
)


def evaluate_event(ts, px, event, horizon, direction):
    target_ts = event["ts"] + horizon * 60
    j = bisect.bisect_left(ts, target_ts, event["i"] + 1)
    if j >= len(ts) or ts[j] > target_ts + 180:
        return None
    entry = event["price"]
    path = px[event["i"]:j + 1]
    exit_px = px[j]
    gross = direction * (exit_px / entry - 1.0) * 100.0
    favorable = [direction * (p / entry - 1.0) * 100.0 for p in path]
    return {
        "gross": gross,
        "net": gross - ROUND_TRIP_COST_PCT,
        "mfe": max(favorable),
        "mae": min(favorable),
    }


def main():
    print(f"LEGACY_REPLAY_V2_START db={DB_PATH}", flush=True)
    if not os.path.exists(DB_PATH):
        print("LEGACY_REPLAY_V2_ERROR database_missing", flush=True)
        return

    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT ts,symbol,price,payload FROM snapshots ORDER BY symbol,ts"
        ).fetchall()

    grouped = defaultdict(list)
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except Exception:
            continue
        grouped[str(row["symbol"])].append({
            "ts": float(row["ts"]),
            "price": float(row["price"]),
            "payload": payload,
        })

    pooled = defaultdict(list)

    for symbol, data in sorted(grouped.items()):
        ts = [x["ts"] for x in data]
        px = [x["price"] for x in data]
        score_state = {}
        score_events = []
        baseline_events = []
        last_baseline_ts = {"TREND_UP": None, "TREND_DOWN": None}
        prev_eligible_regime = None

        for i, item in enumerate(data):
            payload = item["payload"]
            flow = payload.get("trade_flow") or {}
            nd = {k: normalized_delta(flow.get(k)) for k in ("1m", "5m", "15m")}
            stats = {
                "1m": calc_price_stats(ts, px, i, 60),
                "5m": calc_price_stats(ts, px, i, 300),
                "15m": calc_price_stats(ts, px, i, 900),
                "1h": calc_price_stats(ts, px, i, 3600),
                "4h": calc_price_stats(ts, px, i, 14400),
            }
            eligible = (
                payload.get("feed_status") == "live"
                and stats["1h"]["span_seconds"] >= MIN_1H_SPAN
                and stats["4h"]["span_seconds"] >= MIN_4H_SPAN
                and stats["1h"]["samples"] >= MIN_1H_SAMPLES
                and stats["4h"]["samples"] >= MIN_4H_SAMPLES
            )
            regime = classify_regime(stats, nd)
            label = regime["label"] if eligible else None
            oi = payload.get("open_interest") or {}

            if eligible and label in ("TREND_UP", "TREND_DOWN"):
                last_ts = last_baseline_ts[label]
                entering = prev_eligible_regime != label
                cooldown_ok = last_ts is None or item["ts"] - last_ts >= COOLDOWN_SECONDS
                if entering and cooldown_ok:
                    baseline_events.append({
                        "i": i,
                        "ts": item["ts"],
                        "price": item["price"],
                        "regime": label,
                    })
                    last_baseline_ts[label] = item["ts"]

            if eligible:
                prev_eligible_regime = label

            for side, wanted in (("long", "TREND_UP"), ("short", "TREND_DOWN")):
                score = continuation_score(stats, nd, oi, regime, side)
                side_eligible = eligible and label == wanted
                for threshold in THRESHOLDS:
                    key = (side, threshold)
                    prev = score_state.get(key)
                    crossing = bool(
                        prev
                        and side_eligible
                        and prev["eligible"]
                        and prev["score"] < threshold <= score
                    )
                    cooldown_ok = bool(
                        not prev
                        or prev["last_event_ts"] is None
                        or item["ts"] - prev["last_event_ts"] >= COOLDOWN_SECONDS
                    )
                    last_event_ts = prev["last_event_ts"] if prev else None
                    if crossing and cooldown_ok:
                        score_events.append({
                            "i": i,
                            "ts": item["ts"],
                            "price": item["price"],
                            "side": side,
                            "threshold": threshold,
                            "score": score,
                        })
                        last_event_ts = item["ts"]
                    score_state[key] = {
                        "score": score,
                        "eligible": side_eligible,
                        "last_event_ts": last_event_ts,
                    }

        # Baseline: first entry into TREND_UP/TREND_DOWN, with a 4h episode cooldown.
        for regime_label, direction in (("TREND_UP", 1), ("TREND_DOWN", -1)):
            for horizon in HORIZONS_MIN:
                vals = []
                for event in baseline_events:
                    if event["regime"] != regime_label:
                        continue
                    obs = evaluate_event(ts, px, event, horizon, direction)
                    if obs:
                        vals.append(obs)
                        pooled[("baseline", regime_label, horizon)].append(obs)
                print("LEGACY_REPLAY_V2_BASELINE " + json.dumps({
                    "symbol": symbol,
                    "regime": regime_label,
                    "direction": "long" if direction == 1 else "short",
                    "horizon_min": horizon,
                    **summarize(vals),
                }, separators=(",", ":")), flush=True)

        # Long continuation thresholds versus the simple TREND_UP baseline.
        for threshold in THRESHOLDS:
            for horizon in HORIZONS_MIN:
                vals = []
                for event in score_events:
                    if event["side"] != "long" or event["threshold"] != threshold:
                        continue
                    obs = evaluate_event(ts, px, event, horizon, 1)
                    if obs:
                        vals.append(obs)
                        pooled[("continuation_long", threshold, horizon)].append(obs)
                print("LEGACY_REPLAY_V2_LONG " + json.dumps({
                    "symbol": symbol,
                    "threshold": threshold,
                    "horizon_min": horizon,
                    **summarize(vals),
                }, separators=(",", ":")), flush=True)

        # Test whether the failed continuation-short timestamps behave better as reversal longs.
        for threshold in THRESHOLDS:
            for horizon in HORIZONS_MIN:
                short_vals = []
                reversal_vals = []
                for event in score_events:
                    if event["side"] != "short" or event["threshold"] != threshold:
                        continue
                    short_obs = evaluate_event(ts, px, event, horizon, -1)
                    reversal_obs = evaluate_event(ts, px, event, horizon, 1)
                    if short_obs and reversal_obs:
                        short_vals.append(short_obs)
                        reversal_vals.append(reversal_obs)
                        pooled[("continuation_short", threshold, horizon)].append(short_obs)
                        pooled[("reversal_from_short", threshold, horizon)].append(reversal_obs)
                print("LEGACY_REPLAY_V2_SHORT_INVERSION " + json.dumps({
                    "symbol": symbol,
                    "threshold": threshold,
                    "horizon_min": horizon,
                    "continuation_short": summarize(short_vals),
                    "same_timestamp_reversal_long": summarize(reversal_vals),
                }, separators=(",", ":")), flush=True)

        print("LEGACY_REPLAY_V2_META " + json.dumps({
            "symbol": symbol,
            "baseline_events": len(baseline_events),
            "score_events": len(score_events),
            "cost_pct": ROUND_TRIP_COST_PCT,
            "baseline_definition": "first entry into eligible trend regime, 4h cooldown",
        }, separators=(",", ":")), flush=True)

    # Pooled diagnostics.
    for regime_label in ("TREND_UP", "TREND_DOWN"):
        for horizon in HORIZONS_MIN:
            print("LEGACY_REPLAY_V2_POOLED_BASELINE " + json.dumps({
                "regime": regime_label,
                "horizon_min": horizon,
                **summarize(pooled[("baseline", regime_label, horizon)]),
            }, separators=(",", ":")), flush=True)

    for threshold in THRESHOLDS:
        for horizon in HORIZONS_MIN:
            print("LEGACY_REPLAY_V2_POOLED_LONG_COMPARE " + json.dumps({
                "threshold": threshold,
                "horizon_min": horizon,
                "trend_up_baseline": summarize(pooled[("baseline", "TREND_UP", horizon)]),
                "continuation_long": summarize(pooled[("continuation_long", threshold, horizon)]),
            }, separators=(",", ":")), flush=True)
            print("LEGACY_REPLAY_V2_POOLED_SHORT_INVERSION " + json.dumps({
                "threshold": threshold,
                "horizon_min": horizon,
                "trend_down_baseline_short": summarize(pooled[("baseline", "TREND_DOWN", horizon)]),
                "continuation_short": summarize(pooled[("continuation_short", threshold, horizon)]),
                "same_timestamp_reversal_long": summarize(pooled[("reversal_from_short", threshold, horizon)]),
            }, separators=(",", ":")), flush=True)

    print("LEGACY_REPLAY_V2_DONE", flush=True)


if __name__ == "__main__":
    main()
