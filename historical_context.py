from __future__ import annotations

import json
from statistics import median
from typing import Any


HISTORY_VERSION = "0.1.0"
DEFAULT_MAX_ROWS = 240
MIN_DISTRIBUTION_SAMPLES = 30


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_scalar_features(snapshot: dict[str, Any]) -> dict[str, float | None]:
    """Extract scale-free research features suitable for past-only normalization."""
    models = snapshot.get("models") or {}
    features = models.get("features") or {}
    nd = features.get("normalized_delta") or {}
    oi = snapshot.get("open_interest") or {}
    liq = features.get("liquidations") or {}
    book = features.get("book_shape") or {}
    response = features.get("price_response_efficiency") or {}
    stats = snapshot.get("price_stats") or {}
    near = (snapshot.get("order_book") or {}).get("0.1pct") or {}

    # Book imbalance is intentionally omitted when L200 does not actually cover
    # the requested 0.1% band. Otherwise BTC/ETH would get a misleading
    # percentile from only the top few basis points of book depth.
    near_imbalance = near.get("imbalance") if near.get("coverage_complete") else None

    return {
        "delta_norm_1m": _as_float(nd.get("1m")),
        "delta_norm_5m": _as_float(nd.get("5m")),
        "delta_norm_15m": _as_float(nd.get("15m")),
        "flow_accel_1m_minus_5m": _as_float(features.get("flow_acceleration_1m_minus_5m")),
        "flow_accel_5m_minus_15m": _as_float(features.get("flow_acceleration_5m_minus_15m")),
        "activity_burst_1m_vs_5m": _as_float(features.get("activity_burst_1m_vs_5m")),
        "activity_burst_5m_vs_15m": _as_float(features.get("activity_burst_5m_vs_15m")),
        "oi_change_5m_pct": _as_float(oi.get("change_5m_pct")),
        "oi_change_15m_pct": _as_float(oi.get("change_15m_pct")),
        "liquidation_intensity_5m": _as_float(liq.get("intensity_vs_5m_volume")),
        "liquidation_directional_skew": _as_float(liq.get("directional_skew")),
        "book_near_imbalance_complete": _as_float(near_imbalance),
        "book_near_minus_far": _as_float(book.get("near_minus_far")),
        "price_response_efficiency_5m": _as_float(response.get("5m")),
        "price_return_5m_pct": _as_float((stats.get("5m") or {}).get("return_pct")),
    }


def distribution_position(current: float | None, history: list[float]) -> dict[str, Any]:
    """Past-only percentile and robust z-score (median/MAD)."""
    clean = [float(x) for x in history if x is not None]
    result: dict[str, Any] = {
        "current": current,
        "history_samples": len(clean),
        "percentile": None,
        "median": None,
        "mad": None,
        "robust_z": None,
        "eligible": current is not None and len(clean) >= MIN_DISTRIBUTION_SAMPLES,
    }
    if current is None or not clean:
        return result

    med = median(clean)
    mad = median([abs(x - med) for x in clean])
    # Mid-rank percentile handles ties without forcing discrete features to 0/100.
    below = sum(x < current for x in clean)
    equal = sum(x == current for x in clean)
    percentile = (below + 0.5 * equal) / len(clean) * 100
    robust_z = 0.6745 * (current - med) / mad if mad > 0 else None
    result.update({
        "percentile": round(percentile, 2),
        "median": round(med, 6),
        "mad": round(mad, 6),
        "robust_z": round(robust_z, 4) if robust_z is not None else None,
    })
    return result


def _minute_delta_row(snapshot: dict[str, Any]) -> tuple[float, float] | None:
    one = (snapshot.get("trade_flow") or {}).get("1m") or {}
    delta = _as_float(one.get("delta_usd"))
    buy = _as_float(one.get("buy_usd"))
    sell = _as_float(one.get("sell_usd"))
    if delta is None or buy is None or sell is None or buy + sell <= 0:
        return None
    return delta, buy + sell


def stored_cvd_slope(rows: list[tuple[float, dict[str, Any]]],
                     current_snapshot: dict[str, Any],
                     minutes: int = 15) -> dict[str, Any]:
    """Approximate CVD slope from persisted ~1-minute snapshots, not since-start CVD.

    Summing the rolling 1m aggressive delta over minute-spaced snapshots avoids
    the service-restart reset problem of `cvd_since_start_usd`. The result is
    normalized by the same intervals' traded notional and therefore lies near
    [-1, 1] and is comparable across symbols.
    """
    current_ts = _as_float(current_snapshot.get("timestamp"))
    series = [(ts, payload) for ts, payload in rows if current_ts is None or ts < current_ts]
    series = series[-max(0, minutes - 1):]
    if current_ts is not None:
        series.append((current_ts, current_snapshot))

    deltas: list[float] = []
    totals: list[float] = []
    timestamps: list[float] = []
    for ts, payload in series:
        item = _minute_delta_row(payload)
        if item is None:
            continue
        delta, total = item
        deltas.append(delta)
        totals.append(total)
        timestamps.append(ts)

    span = timestamps[-1] - timestamps[0] if len(timestamps) >= 2 else 0.0
    max_gap = max((timestamps[i] - timestamps[i - 1] for i in range(1, len(timestamps))), default=0.0)
    normalized = sum(deltas) / sum(totals) if totals and sum(totals) > 0 else None
    # Require enough separated minute samples to make "15m" meaningful. This is
    # diagnostic metadata; it never bypasses the model's stricter 4h warmup.
    eligible = len(deltas) >= max(10, minutes - 3) and span >= (minutes - 3) * 60 and max_gap <= 180
    return {
        "minutes": minutes,
        "normalized_slope": round(normalized, 5) if normalized is not None else None,
        "samples": len(deltas),
        "span_seconds": round(span, 1),
        "max_gap_seconds": round(max_gap, 1),
        "eligible": eligible,
    }


def build_historical_context(db: Any, snapshot: dict[str, Any],
                             max_rows: int = DEFAULT_MAX_ROWS) -> dict[str, Any]:
    """Build event-time context using only snapshots strictly earlier than now."""
    symbol = str(snapshot.get("symbol") or "")
    ts = _as_float(snapshot.get("timestamp"))
    if not symbol or ts is None:
        return {"version": HISTORY_VERSION, "state": "INVALID_CURRENT_SNAPSHOT"}

    raw_rows = db.execute(
        "SELECT ts,payload FROM snapshots WHERE symbol=? AND ts<? ORDER BY ts DESC LIMIT ?",
        (symbol, ts, max_rows),
    ).fetchall()
    rows: list[tuple[float, dict[str, Any]]] = []
    for row in reversed(raw_rows):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        rows.append((float(row["ts"]), payload))

    current_features = extract_scalar_features(snapshot)
    history_by_feature: dict[str, list[float]] = {key: [] for key in current_features}
    for _, payload in rows:
        extracted = extract_scalar_features(payload)
        for key, value in extracted.items():
            if value is not None:
                history_by_feature[key].append(float(value))

    distributions = {
        key: distribution_position(current, history_by_feature[key])
        for key, current in current_features.items()
    }
    cvd15 = stored_cvd_slope(rows, snapshot, 15)
    return {
        "version": HISTORY_VERSION,
        "state": "OK" if rows else "NO_PRIOR_HISTORY",
        "past_snapshots_considered": len(rows),
        "strictly_past_only": True,
        "distributions": distributions,
        "stored_cvd_slope_15m": cvd15,
        "note": "Historical percentiles/robust-z and stored CVD slope are diagnostics only; they have no score weight yet.",
    }
