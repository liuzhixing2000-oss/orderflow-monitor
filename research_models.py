from __future__ import annotations

from typing import Any

try:
    from .config import settings
except ImportError:  # Flat GitHub upload compatibility.
    from config import settings


MODEL_VERSION = "0.1.1-research"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def normalized_delta(window: dict[str, Any] | None) -> float | None:
    """Aggressive-flow imbalance in [-1, 1], comparable across symbols."""
    if not window:
        return None
    buy = float(window.get("buy_usd") or 0.0)
    sell = float(window.get("sell_usd") or 0.0)
    total = buy + sell
    if total <= 0:
        return None
    return (buy - sell) / total


def activity_burst(short_window: dict[str, Any] | None,
                   long_window: dict[str, Any] | None,
                   short_minutes: float,
                   long_minutes: float) -> float | None:
    """Ratio of per-minute traded notional in the short vs longer window."""
    if not short_window or not long_window:
        return None
    short_total = float(short_window.get("buy_usd") or 0) + float(short_window.get("sell_usd") or 0)
    long_total = float(long_window.get("buy_usd") or 0) + float(long_window.get("sell_usd") or 0)
    if short_total <= 0 or long_total <= 0:
        return None
    return (short_total / short_minutes) / (long_total / long_minutes)


def large_trade_skew(window: dict[str, Any] | None) -> float | None:
    if not window:
        return None
    buys = int(window.get("large_buy_count") or 0)
    sells = int(window.get("large_sell_count") or 0)
    total = buys + sells
    return (buys - sells) / total if total else None


def _positioning_label(price_return_5m: float | None,
                       norm_delta_5m: float | None,
                       oi_change_5m: float | None) -> str:
    if price_return_5m is None or norm_delta_5m is None or oi_change_5m is None:
        return "INSUFFICIENT_DATA"
    p, d, oi = price_return_5m, norm_delta_5m, oi_change_5m
    if p > 0 and d > 0.03 and oi > 0.03:
        return "FRESH_LONG_EXPANSION"
    if p > 0 and d > 0.03 and oi < -0.03:
        return "SHORT_COVERING_OR_DELEVERAGING"
    if p < 0 and d < -0.03 and oi > 0.03:
        return "FRESH_SHORT_EXPANSION"
    if p < 0 and d < -0.03 and oi < -0.03:
        return "LONG_UNWIND_OR_DELEVERAGING"
    if p >= 0 and d < -0.08:
        return "SELL_FLOW_ABSORPTION_CANDIDATE"
    if p <= 0 and d > 0.08:
        return "BUY_FLOW_ABSORPTION_CANDIDATE"
    return "MIXED"


def _regime(features: dict[str, Any]) -> dict[str, Any]:
    stats = features.get("price_stats", {})
    nd = features.get("normalized_delta", {})
    up = 0.0
    down = 0.0
    evidence: list[str] = []

    # Direction and directional efficiency are intentionally separated: a tiny
    # signed return alone should not be treated as a strong trend.
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
            evidence.append(f"{label} price return positive")
            if eff is not None:
                up += efficiency_points * _clamp(eff / 0.35, 0, 1)
        elif ret <= -min_move:
            down += direction_points
            evidence.append(f"{label} price return negative")
            if eff is not None:
                down += efficiency_points * _clamp(eff / 0.35, 0, 1)

    for label, points in (("15m", 15), ("5m", 10)):
        value = nd.get(label)
        if value is None:
            continue
        strength = _clamp(abs(value) / 0.20, 0, 1)
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
        score = min(100, round(up + gap * 0.25))
    elif down >= 60 and down - up >= 20:
        label = "TREND_DOWN"
        score = min(100, round(down + gap * 0.25))
    elif max_side < 45 and low_eff:
        label = "RANGE"
        score = min(100, round(55 + (45 - max_side)))
    else:
        label = "TRANSITION"
        score = min(100, round(45 + gap * 0.5))

    return {
        "label": label,
        "score": score,
        "up_points": up,
        "down_points": down,
        "evidence": evidence,
    }


def _continuation_side(features: dict[str, Any], regime: dict[str, Any], side: str) -> dict[str, Any]:
    sign = 1 if side == "long" else -1
    wanted_regime = "TREND_UP" if side == "long" else "TREND_DOWN"
    nd = features["normalized_delta"]
    stats = features.get("price_stats", {})
    oi = features.get("open_interest", {})
    book = features.get("order_book", {})
    points = 0.0
    evidence: list[str] = []

    if regime.get("label") == wanted_regime:
        points += 25
        evidence.append("higher-timeframe regime aligned")

    nd1 = nd.get("1m")
    nd5 = nd.get("5m")
    nd15 = nd.get("15m")
    r1 = (stats.get("1m") or {}).get("return_pct")
    r5 = (stats.get("5m") or {}).get("return_pct")
    r15 = (stats.get("15m") or {}).get("return_pct")

    # Pullback quality: some counter-flow is welcome only when price damage stays contained.
    if nd5 is not None and sign * nd5 < -0.04:
        if r5 is not None and sign * r5 > -0.25:
            points += 12
            evidence.append("counter-flow pullback with contained price damage")
        if r15 is not None and sign * r15 >= -0.20:
            points += 8
    elif nd15 is not None and sign * nd15 > 0 and r15 is not None and sign * r15 > 0:
        points += 7
        evidence.append("15m flow and price still trend-aligned")

    # Absorption proxy: aggressive opposite flow fails to produce proportional price movement.
    absorption = False
    if nd5 is not None and r5 is not None and sign * nd5 <= -0.10 and sign * r5 >= -0.12:
        points += 20
        absorption = True
        evidence.append("opposite 5m aggressive flow appears absorbed")
    elif nd1 is not None and r1 is not None and sign * nd1 <= -0.18 and sign * r1 >= -0.05:
        points += 10
        absorption = True
        evidence.append("opposite 1m aggressive flow has weak price response")

    # Re-acceleration trigger. 1m is a trigger, not the higher-timeframe thesis.
    reaccel = 0.0
    if nd1 is not None and sign * nd1 >= 0.08:
        reaccel += 10
    if nd1 is not None and nd5 is not None and sign * (nd1 - nd5) >= 0.10:
        reaccel += 6
    if nd5 is not None and nd15 is not None and sign * nd5 > 0 and sign * nd5 >= sign * nd15 - 0.02:
        reaccel += 4
    if reaccel:
        points += min(20, reaccel)
        evidence.append("short-horizon flow re-accelerating with trend")

    # OI quality distinguishes fresh positioning from covering/deleveraging.
    oi5 = oi.get("change_5m_pct")
    if oi5 is not None:
        if oi5 >= 0.08 and nd5 is not None and sign * nd5 > 0.03:
            points += 10
            evidence.append("OI expansion confirms directional flow")
        elif oi5 > 0:
            points += 6
        elif oi5 <= -0.08 and nd5 is not None and sign * nd5 > 0.03:
            points += 2
            evidence.append("directional move is accompanied by falling OI; covering/deleveraging risk")
        else:
            points += 4

    # Book is deliberately low weight and is discounted if the requested depth
    # is not fully covered by the subscribed level count.
    near = book.get("0.1pct") or {}
    imb = near.get("imbalance")
    coverage_complete = near.get("coverage_complete")
    if imb is not None and sign * imb >= 0.12:
        book_points = 5 if coverage_complete else 2
        points += book_points
        evidence.append("near-book depth supports direction" if coverage_complete else "top-book supports direction but depth coverage is incomplete")

    score = int(round(_clamp(points, 0, 100)))
    if regime.get("label") != wanted_regime:
        state = "DISABLED_BY_REGIME"
    elif score >= 80:
        state = "WATCH_LONG" if side == "long" else "WATCH_SHORT"
    elif score >= 65:
        state = "FORMING_LONG" if side == "long" else "FORMING_SHORT"
    else:
        state = "NONE"

    return {"score": score, "state": state, "absorption": absorption, "evidence": evidence}


def _data_quality(snapshot: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    age = snapshot.get("last_update_age_seconds")
    feed_status = snapshot.get("feed_status")
    stat1 = stats.get("1h") or {}
    stat4 = stats.get("4h") or {}
    span1 = float(stat1.get("span_seconds") or 0.0)
    span4 = float(stat4.get("span_seconds") or 0.0)
    samples1 = int(stat1.get("samples") or 0)
    samples4 = int(stat4.get("samples") or 0)

    if feed_status != "live" or age is None or age > 5:
        state = "STALE_FEED"
    elif span4 < settings.research_min_4h_span_seconds:
        state = "WARMUP_4H"
    elif span1 < settings.research_min_1h_span_seconds:
        state = "WARMUP_1H"
    elif samples4 < settings.research_min_4h_samples or samples1 < settings.research_min_1h_samples:
        state = "GAPPED_HISTORY"
    else:
        state = "OK"

    return {
        "state": state,
        "eligible": state == "OK",
        "price_samples_1h": samples1,
        "price_samples_4h": samples4,
        "span_1h_seconds": round(span1, 1),
        "span_4h_seconds": round(span4, 1),
        "required_span_1h_seconds": settings.research_min_1h_span_seconds,
        "required_span_4h_seconds": settings.research_min_4h_span_seconds,
        "required_samples_1h": settings.research_min_1h_samples,
        "required_samples_4h": settings.research_min_4h_samples,
    }


def build_research_models(snapshot: dict[str, Any]) -> dict[str, Any]:
    flow = snapshot.get("trade_flow", {})
    stats = snapshot.get("price_stats", {})

    nd = {k: normalized_delta(flow.get(k)) for k in ("1m", "5m", "15m")}
    features = {
        "normalized_delta": {k: round(v, 4) if v is not None else None for k, v in nd.items()},
        "flow_acceleration_1m_minus_5m": round(nd["1m"] - nd["5m"], 4) if nd["1m"] is not None and nd["5m"] is not None else None,
        "flow_acceleration_5m_minus_15m": round(nd["5m"] - nd["15m"], 4) if nd["5m"] is not None and nd["15m"] is not None else None,
        "activity_burst_1m_vs_5m": activity_burst(flow.get("1m"), flow.get("5m"), 1, 5),
        "activity_burst_5m_vs_15m": activity_burst(flow.get("5m"), flow.get("15m"), 5, 15),
        "large_trade_skew_15m": large_trade_skew(flow.get("15m")),
        "price_stats": stats,
        "open_interest": snapshot.get("open_interest", {}),
        "order_book": snapshot.get("order_book", {}),
    }
    for key in ("activity_burst_1m_vs_5m", "activity_burst_5m_vs_15m", "large_trade_skew_15m"):
        if isinstance(features[key], float):
            features[key] = round(features[key], 4)

    p5 = (stats.get("5m") or {}).get("return_pct")
    features["positioning_5m"] = _positioning_label(
        p5, nd.get("5m"), (snapshot.get("open_interest") or {}).get("change_5m_pct")
    )

    quality = _data_quality(snapshot, stats)
    raw_regime = _regime(features)
    long = _continuation_side(features, raw_regime, "long")
    short = _continuation_side(features, raw_regime, "short")

    # Never publish a usable HTF regime before the clock-time warmup has really
    # completed. The raw classifier is retained only as diagnostics so that a
    # dense few minutes of ticks cannot masquerade as a 4h trend.
    if quality["eligible"]:
        regime = {**raw_regime, "eligible": True, "raw_label": raw_regime["label"]}
    else:
        regime = {
            **raw_regime,
            "label": "WARMUP" if quality["state"].startswith("WARMUP") else "UNAVAILABLE",
            "raw_label": raw_regime["label"],
            "eligible": False,
        }
        long["state"] = "DATA_QUALITY_BLOCK"
        short["state"] = "DATA_QUALITY_BLOCK"

    return {
        "version": MODEL_VERSION,
        "data_quality": quality,
        "features": features,
        "regime": regime,
        "continuation": {
            "long": long["score"],
            "short": short["score"],
            "long_state": long["state"],
            "short_state": short["state"],
            "long_evidence": long["evidence"],
            "short_evidence": short["evidence"],
            "long_absorption": long["absorption"],
            "short_absorption": short["absorption"],
        },
        "reversal": {"status": "NOT_IMPLEMENTED_IN_V0.1"},
        "range": {"status": "NOT_IMPLEMENTED_IN_V0.1"},
        "warning": "Research hypothesis only; scores are not validated trading signals.",
    }
