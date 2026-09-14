from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Any


def parse_liquidation_map(payload: dict[str, Any]) -> list[dict[str, float | None]]:
    """Normalize CoinGlass aggregated-map rows into price/amount/leverage records."""
    if str(payload.get("code")) != "0":
        raise ValueError(payload.get("msg") or "CoinGlass returned an error")
    node: Any = payload.get("data", {})
    if isinstance(node, dict) and isinstance(node.get("data"), dict):
        node = node["data"]
    if not isinstance(node, dict):
        raise ValueError("Unexpected CoinGlass liquidation-map payload")

    rows: list[dict[str, float | None]] = []
    for raw_price, values in node.items():
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, (list, tuple)) or len(value) < 2:
                continue
            try:
                price = float(value[0] if value[0] is not None else raw_price)
                amount = float(value[1])
                leverage = float(value[2]) if len(value) > 2 and value[2] is not None else None
            except (TypeError, ValueError):
                continue
            if price > 0 and amount > 0:
                rows.append({"price": price, "amount_usd": amount, "leverage": leverage})
    return rows


def _cluster_side(
    rows: list[dict[str, float | None]], current_price: float, band_pct: float
) -> list[dict[str, Any]]:
    bucket_size = max(current_price * band_pct / 100.0, 1e-9)
    buckets: dict[int, dict[str, Any]] = {}
    for row in rows:
        price, amount = float(row["price"]), float(row["amount_usd"])
        bucket = round(price / bucket_size)
        item = buckets.setdefault(
            bucket,
            {"amount_usd": 0.0, "weighted_price": 0.0, "leverage_usd": defaultdict(float)},
        )
        item["amount_usd"] += amount
        item["weighted_price"] += price * amount
        leverage = row.get("leverage")
        if leverage is not None:
            item["leverage_usd"][str(int(leverage))] += amount

    clusters = []
    for item in buckets.values():
        amount = item["amount_usd"]
        price = item["weighted_price"] / amount
        leverage_mix = {
            key: round(value, 2)
            for key, value in sorted(item["leverage_usd"].items(), key=lambda x: -x[1])
        }
        clusters.append(
            {
                "price": round(price, 6),
                "distance_pct": round(abs(price / current_price - 1.0) * 100.0, 3),
                "amount_usd": round(amount, 2),
                "leverage_mix_usd": leverage_mix,
            }
        )
    return clusters


def summarize_liquidation_map(
    payload: dict[str, Any],
    current_price: float,
    *,
    max_distance_pct: float = 8.0,
    cluster_band_pct: float = 0.25,
    top_n: int = 3,
) -> dict[str, Any]:
    rows = parse_liquidation_map(payload)
    nearby = [
        row for row in rows
        if abs(float(row["price"]) / current_price - 1.0) * 100.0 <= max_distance_pct
    ]
    above = _cluster_side([row for row in nearby if float(row["price"]) > current_price], current_price, cluster_band_pct)
    below = _cluster_side([row for row in nearby if float(row["price"]) < current_price], current_price, cluster_band_pct)

    def side_summary(clusters: list[dict[str, Any]], forced_flow: str) -> dict[str, Any]:
        total = sum(float(x["amount_usd"]) for x in clusters)
        ranked = sorted(clusters, key=lambda x: float(x["amount_usd"]), reverse=True)
        max_amount = float(ranked[0]["amount_usd"]) if ranked else 0.0
        for cluster in ranked:
            strength = float(cluster["amount_usd"]) / max_amount if max_amount else 0.0
            distance = max(float(cluster["distance_pct"]), 0.05)
            cluster["attraction_score"] = round(100.0 * strength / (1.0 + distance), 1)
        primary = max(ranked, key=lambda x: x["attraction_score"], default=None)
        return {
            "forced_flow": forced_flow,
            "total_nearby_usd": round(total, 2),
            "primary_target": primary,
            "largest_clusters": ranked[:top_n],
        }

    up = side_summary(above, "buy_to_close_shorts")
    down = side_summary(below, "sell_to_close_longs")
    up_total, down_total = up["total_nearby_usd"], down["total_nearby_usd"]
    denom = up_total + down_total
    return {
        "status": "live",
        "source": "coinglass_aggregated_liquidation_map",
        "current_price": current_price,
        "max_distance_pct": max_distance_pct,
        "cluster_band_pct": cluster_band_pct,
        "upside_short_liquidations": up,
        "downside_long_liquidations": down,
        "nearby_liquidation_imbalance": round((up_total - down_total) / denom, 4) if denom else None,
        "raw_level_count": len(nearby),
        "fetched_at": time.time(),
        "warning": "清算地图是模型估算的潜在强平区，不是挂单，也不能单独证明主力意图。",
    }


def assess_squeeze_path(
    liquidation_map: dict[str, Any],
    *,
    trade_flow: dict[str, dict[str, Any]],
    structure: dict[str, Any],
    order_book: dict[str, Any],
    oi_change_15m: float | None,
    liquidations_5m: dict[str, float],
    price_change_5m_pct: float | None,
) -> dict[str, Any]:
    if liquidation_map.get("status") != "live":
        return {
            "status": "unavailable",
            "upside_reachability_score": None,
            "downside_reachability_score": None,
            "inferred_pressure": "无法评估",
            "reason": liquidation_map.get("reason", "liquidation map unavailable"),
        }

    def score(direction: str) -> tuple[int, list[str], str]:
        upward = direction == "up"
        side_key = "upside_short_liquidations" if upward else "downside_long_liquidations"
        target = liquidation_map[side_key].get("primary_target")
        if not target:
            return 0, ["附近没有可识别的清算密集区"], "none"

        distance = float(target["distance_pct"])
        amount = float(target["amount_usd"])
        attraction = float(target["attraction_score"])
        points = min(30.0, attraction * 0.30)
        evidence = [f"目标 {target['price']}，距离 {distance:.2f}%，估算清算 ${amount:,.0f}"]

        for timeframe, weight in (("4h", 9), ("1h", 7)):
            trend = structure.get(timeframe, {}).get("trend")
            if trend == ("up" if upward else "down"):
                points += weight
                evidence.append(f"{timeframe}趋势同向")

        for timeframe, weight in (("15m", 8), ("5m", 10), ("1m", 5)):
            ratio = trade_flow.get(timeframe, {}).get("buy_ratio")
            if ratio is None:
                continue
            directional = ratio if upward else 1.0 - ratio
            if directional >= 0.55:
                points += weight * min(1.0, (directional - 0.5) / 0.18)
                if timeframe in ("15m", "5m"):
                    evidence.append(f"{timeframe}主动{'买' if upward else '卖'}盘配合")

        imbalance = order_book.get("0.1pct", {}).get("imbalance")
        if imbalance is not None and (imbalance > 0.12 if upward else imbalance < -0.12):
            points += min(7.0, abs(float(imbalance)) * 12.0)
            evidence.append(f"近端盘口{'买' if upward else '卖'}方占优")

        forced = float(liquidations_5m.get("short_usd" if upward else "long_usd", 0.0))
        if forced > 0:
            points += min(10.0, 2.0 + math.log10(1.0 + forced))
            evidence.append(f"5分钟已出现${forced:,.0f}{'空单' if upward else '多单'}清算")

        moving_toward = (
            price_change_5m_pct is not None
            and (price_change_5m_pct > 0 if upward else price_change_5m_pct < 0)
        )
        if oi_change_15m is not None and oi_change_15m < -0.10 and moving_toward:
            points += 10
            evidence.append("价格方向与OI下降组合符合平仓/清算推进")

        points = round(min(100.0, points))
        phase = "triggering" if distance <= 0.5 and points >= 65 else "approaching" if distance <= 2.0 and points >= 50 else "dormant"
        return points, evidence, phase

    up_score, up_evidence, up_phase = score("up")
    down_score, down_evidence, down_phase = score("down")
    gap = up_score - down_score
    if gap >= 15:
        pressure = "上推/逼空迹象较强"
    elif gap <= -15:
        pressure = "下压/多杀多迹象较强"
    else:
        pressure = "没有清晰的单边推进迹象"
    confidence = "高" if abs(gap) >= 30 and max(up_score, down_score) >= 70 else "中" if abs(gap) >= 15 else "低"
    return {
        "status": "research_heuristic",
        "upside_reachability_score": up_score,
        "downside_reachability_score": down_score,
        "inferred_pressure": pressure,
        "confidence": confidence,
        "upside_phase": up_phase,
        "downside_phase": down_phase,
        "upside_evidence": up_evidence,
        "downside_evidence": down_evidence,
        "interpretation": "分数表示当前订单流是否正在配合价格走向清算区，不是统计校准后的概率。",
    }
