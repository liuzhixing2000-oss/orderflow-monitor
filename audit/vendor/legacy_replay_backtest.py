from __future__ import annotations

import bisect
import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median

DB_PATH = os.getenv("DATA_PATH", "/data/orderflow.db")
THRESHOLDS = (50, 60, 70, 80, 90)
HORIZONS_MIN = (15, 30, 60, 240)
COOLDOWN_SECONDS = 4 * 60 * 60
ROUND_TRIP_COST_PCT = 0.12
MIN_1H_SPAN = 3420
MIN_4H_SPAN = 13680
MIN_1H_SAMPLES = 45
MIN_4H_SAMPLES = 180


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def normalized_delta(window: dict | None) -> float | None:
    if not window:
        return None
    buy = float(window.get("buy_usd") or 0.0)
    sell = float(window.get("sell_usd") or 0.0)
    total = buy + sell
    return (buy - sell) / total if total > 0 else None


def calc_price_stats(ts: list[float], px: list[float], i: int, seconds: int) -> dict:
    cutoff = ts[i] - seconds
    j = bisect.bisect_left(ts, cutoff, 0, i + 1)
    samples = i - j + 1
    span = ts[i] - ts[j] if samples >= 2 else 0.0
    if samples < 2 or px[j] == 0:
        return {"return_pct": None, "efficiency": None, "samples": samples, "span_seconds": span}
    path = sum(abs(px[k] - px[k - 1]) for k in range(j + 1, i + 1))
    net = abs(px[i] - px[j])
    eff = net / path if path else 0.0
    return {
        "return_pct": (px[i] / px[j] - 1.0) * 100.0,
        "efficiency": eff,
        "samples": samples,
        "span_seconds": span,
    }


def classify_regime(stats: dict, nd: dict) -> dict:
    up = 0.0
    down = 0.0
    for label, direction_points, efficiency_points, min_move in (
        ("4h", 20, 15, 0.15),
        ("1h", 15, 10, 0.05),
    ):
        stat = stats.get(label) or {}
        ret = stat.get("return_pct")
        eff = stat.get("efficiency")
        if ret is None:
            continue
        if ret >= min_move:
            up += direction_points
            if eff is not None:
                up += efficiency_points * clamp(eff / 0.35, 0, 1)
        elif ret <= -min_move:
            down += direction_points
            if eff is not None:
                down += efficiency_points * clamp(eff / 0.35, 0, 1)

    for label, points in (("15m", 15), ("5m", 10)):
        value = nd.get(label)
        if value is None:
            continue
        strength = clamp(abs(value) / 0.20, 0, 1)
        if value > 0:
            up += points * strength
        elif value < 0:
            down += points * strength

    up = round(up, 1)
    down = round(down, 1)
    max_side = max(up, down)
    gap = abs(up - down)
    eff1 = (stats.get("1h") or {}).get("efficiency")
    eff4 = (stats.get("4h") or {}).get("efficiency")
    low_eff = all(x is not None and x < 0.22 for x in (eff1, eff4))

    if up >= 60 and up - down >= 20:
        label = "TREND_UP"
    elif down >= 60 and down - up >= 20:
        label = "TREND_DOWN"
    elif max_side < 45 and low_eff:
        label = "RANGE"
    else:
        label = "TRANSITION"
    return {"label": label, "up_points": up, "down_points": down, "gap": gap}


def continuation_score(stats: dict, nd: dict, oi: dict, regime: dict, side: str) -> int:
    sign = 1 if side == "long" else -1
    wanted = "TREND_UP" if side == "long" else "TREND_DOWN"
    points = 0.0

    if regime["label"] == wanted:
        points += 25

    nd1, nd5, nd15 = nd.get("1m"), nd.get("5m"), nd.get("15m")
    r1 = (stats.get("1m") or {}).get("return_pct")
    r5 = (stats.get("5m") or {}).get("return_pct")
    r15 = (stats.get("15m") or {}).get("return_pct")

    if nd5 is not None and sign * nd5 < -0.04:
        if r5 is not None and sign * r5 > -0.25:
            points += 12
        if r15 is not None and sign * r15 >= -0.20:
            points += 8
    elif nd15 is not None and sign * nd15 > 0 and r15 is not None and sign * r15 > 0:
        points += 7

    if nd5 is not None and r5 is not None and sign * nd5 <= -0.10 and sign * r5 >= -0.12:
        points += 20
    elif nd1 is not None and r1 is not None and sign * nd1 <= -0.18 and sign * r1 >= -0.05:
        points += 10

    reaccel = 0.0
    if nd1 is not None and sign * nd1 >= 0.08:
        reaccel += 10
    if nd1 is not None and nd5 is not None and sign * (nd1 - nd5) >= 0.10:
        reaccel += 6
    if nd5 is not None and nd15 is not None and sign * nd5 > 0 and sign * nd5 >= sign * nd15 - 0.02:
        reaccel += 4
    points += min(20, reaccel)

    oi5 = oi.get("change_5m_pct")
    if oi5 is not None:
        oi5 = float(oi5)
        if oi5 >= 0.08 and nd5 is not None and sign * nd5 > 0.03:
            points += 10
        elif oi5 > 0:
            points += 6
        elif oi5 <= -0.08 and nd5 is not None and sign * nd5 > 0.03:
            points += 2
        else:
            points += 4

    # Deliberately exclude historical order-book points: BTC/ETH L50 coverage was incomplete.
    return int(round(clamp(points, 0, 100)))


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def summarize(values: list[dict]) -> dict:
    if not values:
        return {"n": 0, "avg_net": None, "median_net": None, "win_rate": None, "pf": None, "avg_mfe": None, "avg_mae": None}
    nets = [x["net"] for x in values]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    pf = None
    if losses and sum(losses) != 0:
        pf = sum(wins) / abs(sum(losses))
    return {
        "n": len(values),
        "avg_net": round(sum(nets) / len(nets), 5),
        "median_net": round(median(nets), 5),
        "win_rate": round(len(wins) / len(values), 4),
        "pf": round(pf, 3) if pf is not None else None,
        "avg_mfe": round(sum(x["mfe"] for x in values) / len(values), 5),
        "avg_mae": round(sum(x["mae"] for x in values) / len(values), 5),
    }


def main() -> None:
    print(f"LEGACY_REPLAY_START db={DB_PATH}", flush=True)
    if not os.path.exists(DB_PATH):
        print("LEGACY_REPLAY_ERROR database_missing", flush=True)
        return

    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT ts,symbol,price,payload FROM snapshots ORDER BY symbol,ts"
        ).fetchall()

    grouped: dict[str, list[dict]] = defaultdict(list)
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

    all_outcomes: dict[tuple[str, int, int], list[dict]] = defaultdict(list)

    for symbol, data in sorted(grouped.items()):
        ts = [x["ts"] for x in data]
        px = [x["price"] for x in data]
        regime_counts = Counter()
        eligible_rows = 0
        score_max = {"long": 0, "short": 0}
        event_state: dict[tuple[str, int], dict] = {}
        events: list[dict] = []

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
            if eligible:
                regime_counts[regime["label"]] += 1
                eligible_rows += 1
            oi = payload.get("open_interest") or {}

            for side, wanted in (("long", "TREND_UP"), ("short", "TREND_DOWN")):
                score = continuation_score(stats, nd, oi, regime, side)
                score_max[side] = max(score_max[side], score)
                side_eligible = eligible and regime["label"] == wanted
                for threshold in THRESHOLDS:
                    key = (side, threshold)
                    prev = event_state.get(key)
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
                        events.append({
                            "i": i,
                            "ts": item["ts"],
                            "price": item["price"],
                            "side": side,
                            "threshold": threshold,
                            "score": score,
                        })
                        last_event_ts = item["ts"]
                    event_state[key] = {
                        "score": score,
                        "eligible": side_eligible,
                        "last_event_ts": last_event_ts,
                    }

        first_ts = ts[0] if ts else 0
        last_ts = ts[-1] if ts else 0
        print("LEGACY_REPLAY_META " + json.dumps({
            "symbol": symbol,
            "rows": len(data),
            "first_utc": iso(first_ts) if first_ts else None,
            "last_utc": iso(last_ts) if last_ts else None,
            "span_hours": round((last_ts - first_ts) / 3600, 2) if first_ts else 0,
            "eligible_rows": eligible_rows,
            "regimes": dict(regime_counts),
            "max_score": score_max,
            "events": len(events),
            "book_weight": 0,
            "cost_pct": ROUND_TRIP_COST_PCT,
        }, separators=(",", ":")), flush=True)

        outcomes: dict[tuple[str, int, int], list[dict]] = defaultdict(list)
        for event in events:
            direction = 1 if event["side"] == "long" else -1
            entry_i = event["i"]
            entry = event["price"]
            for horizon in HORIZONS_MIN:
                target_ts = event["ts"] + horizon * 60
                j = bisect.bisect_left(ts, target_ts, entry_i + 1)
                if j >= len(ts) or ts[j] > target_ts + 180:
                    continue
                path = px[entry_i:j + 1]
                exit_px = px[j]
                net = direction * (exit_px / entry - 1.0) * 100.0 - ROUND_TRIP_COST_PCT
                favorable = [direction * (p / entry - 1.0) * 100.0 for p in path]
                obs = {
                    "net": net,
                    "mfe": max(favorable),
                    "mae": min(favorable),
                }
                key = (event["side"], event["threshold"], horizon)
                outcomes[key].append(obs)
                all_outcomes[(event["side"], event["threshold"], horizon)].append(obs)

        for side in ("long", "short"):
            for horizon in HORIZONS_MIN:
                threshold_rows = []
                for threshold in THRESHOLDS:
                    result = summarize(outcomes[(side, threshold, horizon)])
                    threshold_rows.append({"threshold": threshold, **result})
                print("LEGACY_REPLAY_RESULT " + json.dumps({
                    "symbol": symbol,
                    "side": side,
                    "horizon_min": horizon,
                    "thresholds": threshold_rows,
                }, separators=(",", ":")), flush=True)

    for side in ("long", "short"):
        for horizon in HORIZONS_MIN:
            threshold_rows = []
            for threshold in THRESHOLDS:
                threshold_rows.append({
                    "threshold": threshold,
                    **summarize(all_outcomes[(side, threshold, horizon)]),
                })
            print("LEGACY_REPLAY_POOLED " + json.dumps({
                "side": side,
                "horizon_min": horizon,
                "thresholds": threshold_rows,
            }, separators=(",", ":")), flush=True)

    print("LEGACY_REPLAY_DONE", flush=True)


if __name__ == "__main__":
    main()
