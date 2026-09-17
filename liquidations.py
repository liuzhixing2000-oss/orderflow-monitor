"""
Multi-exchange liquidation aggregation with regime classification.

Supports:
- Bybit allLiquidation feed (USD-M futures, preserve native format)
- Binance USD-M !forceOrder@arr (free public stream, with reconnect isolation)
- Regime classification (long-liquidation deleveraging, short squeeze, fresh-position-building)
- Per-exchange and aggregate liquidation tracking
- Exchange metadata on events
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

try:
    from .config import settings
except ImportError:
    from config import settings

log = logging.getLogger(__name__)


@dataclass
class LiquidationEvent:
    """Liquidation event with exchange metadata."""
    timestamp: float
    exchange: str  # "bybit" or "binance"
    symbol: str    # "BTCUSDT" etc
    side: str      # "buy" (closes longs) or "sell" (closes shorts)
    price: float
    quantity: float
    usd_value: float


@dataclass
class LiquidationRegime:
    """Classified liquidation regime based on 15m metrics."""
    regime: str  # "long_deleveraging", "short_squeeze", "fresh_position_building", "unknown"
    confidence: float  # 0-1 confidence score
    price_change_15m_pct: float | None
    oi_change_15m_pct: float | None
    delta_15m_usd: float | None
    liquidation_side: str  # "long" or "short"
    evidence: list[str]


class LiquidationAggregator:
    """Aggregates liquidations across exchanges with regime detection."""

    def __init__(self, symbol: str, history_seconds: int = 3600):
        self.symbol = symbol
        self.history_seconds = history_seconds
        # Per-exchange deques: (ts, side, price, qty, usd_value, exchange)
        self.bybit_liquidations: deque = deque()
        self.binance_liquidations: deque = deque()
        # Aggregated: (ts, side, usd_value, exchange)
        self.all_liquidations: deque = deque()
        self.last_regime: LiquidationRegime | None = None

    def add_bybit(self, ts: float, side: str, price: float, qty: float) -> None:
        """Add Bybit liquidation event (from allLiquidation feed)."""
        usd_value = price * qty
        self.bybit_liquidations.append((ts, side, price, qty, usd_value))
        self.all_liquidations.append((ts, side, usd_value, "bybit"))
        self.prune()

    def add_binance(self, ts: float, side: str, price: float, qty: float) -> None:
        """Add Binance liquidation event (from !forceOrder@arr feed)."""
        usd_value = price * qty
        self.binance_liquidations.append((ts, side, price, qty, usd_value))
        self.all_liquidations.append((ts, side, usd_value, "binance"))
        self.prune()

    def prune(self) -> None:
        """Remove events older than history_seconds."""
        cutoff = time.time() - self.history_seconds
        for series in (self.bybit_liquidations, self.binance_liquidations, self.all_liquidations):
            while series and series[0][0] < cutoff:
                series.popleft()

    def window_by_exchange(self, seconds: int) -> dict[str, dict[str, float]]:
        """Liquidation volumes in a time window, broken down by exchange."""
        cutoff = time.time() - seconds
        result = {}
        for exchange, series in (
            ("bybit", self.bybit_liquidations),
            ("binance", self.binance_liquidations),
        ):
            rows = [x for x in series if x[0] >= cutoff]
            long_usd = sum(x[4] for x in rows if x[1].lower() == "sell")
            short_usd = sum(x[4] for x in rows if x[1].lower() == "buy")
            result[exchange] = {
                "long_usd": round(long_usd, 2),
                "short_usd": round(short_usd, 2),
                "total_usd": round(long_usd + short_usd, 2),
                "event_count": len(rows),
            }
        # Aggregate across all exchanges
        rows = [x for x in self.all_liquidations if x[0] >= cutoff]
        long_usd = sum(x[2] for x in rows if x[1].lower() == "sell")
        short_usd = sum(x[2] for x in rows if x[1].lower() == "buy")
        result["aggregate"] = {
            "long_usd": round(long_usd, 2),
            "short_usd": round(short_usd, 2),
            "total_usd": round(long_usd + short_usd, 2),
            "event_count": len(rows),
        }
        return result

    def classify_regime(
        self,
        price_change_15m_pct: float | None,
        oi_change_15m_pct: float | None,
        delta_15m_usd: float | None,
        trade_flow_15m: dict[str, Any] | None = None,
    ) -> LiquidationRegime:
        """
        Classify liquidation regime based on 15m metrics.
        
        - long_deleveraging: shorts being liquidated + long OI decreasing + price falling
          → longs exiting, short squeeze abating
        - short_squeeze: longs being liquidated + OI rising/stable + price rising
          → longs forced out, shorts profiting
        - fresh_position_building: moderate liquidations + OI rising + delta positive
          → new longs accumulating through liquidation volatility
        """
        liq_15m = self.window_by_exchange(900)
        agg = liq_15m.get("aggregate", {})
        long_liq = agg.get("long_usd", 0)
        short_liq = agg.get("short_usd", 0)

        evidence: list[str] = []
        regime = "unknown"
        confidence = 0.0

        # Determine which side is being liquidated more
        if long_liq > short_liq:
            liquidation_side = "long"
            liq_dominance = (long_liq - short_liq) / (long_liq + short_liq + 1) if (long_liq + short_liq) else 0
        else:
            liquidation_side = "short"
            liq_dominance = (short_liq - long_liq) / (long_liq + short_liq + 1) if (long_liq + short_liq) else 0

        # Confidence ramps: more liquidation events = more signal
        base_confidence = min(0.6, agg.get("event_count", 0) / 50.0)

        # Case 1: short being liquidated (long closing or short covering)
        if liquidation_side == "short":
            evidence.append(f"Shorts liquidated ${long_liq:,.0f}")
            if price_change_15m_pct is not None and price_change_15m_pct > 0:
                evidence.append(f"Price up {price_change_15m_pct:.2f}%")
                base_confidence += 0.15
            if oi_change_15m_pct is not None:
                if oi_change_15m_pct > 0.05:
                    # Rising OI + shorts liquidated = longs being added
                    regime = "fresh_position_building"
                    confidence = min(1.0, base_confidence + 0.2)
                    evidence.append(f"OI up {oi_change_15m_pct:.2f}% → fresh longs")
                elif oi_change_15m_pct < -0.05:
                    # Falling OI + shorts liquidated = mixed (could be squeeze subsiding)
                    regime = "short_squeeze"
                    confidence = min(1.0, base_confidence + 0.1)
                    evidence.append(f"OI down {oi_change_15m_pct:.2f}% after shorts liquidated")
                else:
                    regime = "short_squeeze"
                    confidence = min(1.0, base_confidence + 0.05)
                    evidence.append("OI stable during short liquidation")
            else:
                if price_change_15m_pct is not None and price_change_15m_pct > 0.5:
                    regime = "short_squeeze"
                    confidence = min(1.0, base_confidence + 0.1)
                    evidence.append("Strong price rise, shorts liquidated")

        # Case 2: long being liquidated (short covering or long deleveraging)
        elif liquidation_side == "long":
            evidence.append(f"Longs liquidated ${short_liq:,.0f}")
            if price_change_15m_pct is not None and price_change_15m_pct < 0:
                evidence.append(f"Price down {price_change_15m_pct:.2f}%")
                base_confidence += 0.15
            if oi_change_15m_pct is not None:
                if oi_change_15m_pct < -0.10:
                    # Falling OI + longs liquidated = deleveraging
                    regime = "long_deleveraging"
                    confidence = min(1.0, base_confidence + 0.25)
                    evidence.append(f"OI down {oi_change_15m_pct:.2f}% → longs exiting")
                elif oi_change_15m_pct > 0.05:
                    # Rising OI + longs liquidated = shorts accumulating
                    regime = "short_squeeze"
                    confidence = min(1.0, base_confidence + 0.15)
                    evidence.append(f"OI up {oi_change_15m_pct:.2f}% despite long liquidations")
                else:
                    regime = "long_deleveraging"
                    confidence = min(1.0, base_confidence + 0.1)
                    evidence.append("OI stable, longs liquidating (deleveraging)")
            else:
                if price_change_15m_pct is not None and price_change_15m_pct < -0.5:
                    regime = "long_deleveraging"
                    confidence = min(1.0, base_confidence + 0.15)
                    evidence.append("Strong price decline, longs liquidated")

        # Fallback: infer from delta if regime still unknown
        if regime == "unknown" and delta_15m_usd is not None:
            if abs(delta_15m_usd) > 50_000:
                if delta_15m_usd > 0 and liquidation_side == "short":
                    regime = "fresh_position_building"
                    confidence = 0.4
                    evidence.append(f"Strong buy flow ${delta_15m_usd:,.0f}, shorts liquidated")
                elif delta_15m_usd < 0 and liquidation_side == "long":
                    regime = "long_deleveraging"
                    confidence = 0.4
                    evidence.append(f"Strong sell flow ${delta_15m_usd:,.0f}, longs liquidated")

        self.last_regime = LiquidationRegime(
            regime=regime if regime != "unknown" else "unknown",
            confidence=round(confidence, 3),
            price_change_15m_pct=price_change_15m_pct,
            oi_change_15m_pct=oi_change_15m_pct,
            delta_15m_usd=delta_15m_usd,
            liquidation_side=liquidation_side,
            evidence=evidence,
        )
        return self.last_regime


class BinanceLiquidationFeed:
    """
    Reconnect-isolated Binance USD-M !forceOrder@arr stream.
    
    Free public data, no API key required.
    Reconnects on failure with exponential backoff.
    """

    def __init__(self, symbol: str, on_liquidation: callable, max_retries: int = 5):
        """
        Args:
            symbol: e.g. "BTCUSDT" (normalized uppercase)
            on_liquidation: callable(ts, side, price, qty) -> None
            max_retries: max reconnection attempts before giving up for this burst
        """
        self.symbol = symbol
        self.on_liquidation = on_liquidation
        self.max_retries = max_retries
        self.connected = False
        self.stream_url = f"wss://fstream.binance.com/ws/{self.symbol.lower()}@forceOrder@arr"
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the feed. Run in background."""
        self.task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        """Stop the feed gracefully."""
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _run_forever(self) -> None:
        """Connect and process liquidation events indefinitely."""
        retry_count = 0
        backoff = 0.5

        while True:
            try:
                async with websockets.connect(self.stream_url, ping_interval=30, ping_timeout=10) as ws:
                    self.connected = True
                    log.info("Binance liquidation feed connected: %s", self.symbol)
                    retry_count = 0
                    backoff = 0.5

                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                            # Binance sends { "o": { ... } } for force order
                            force_order = data.get("o", {})
                            if not force_order:
                                continue

                            ts = float(force_order.get("T", time.time() * 1000)) / 1000.0
                            side = force_order.get("S", "").upper()  # BUY or SELL
                            price = float(force_order.get("p", 0))
                            qty = float(force_order.get("q", 0))

                            if price > 0 and qty > 0 and side in ("BUY", "SELL"):
                                self.on_liquidation(ts, side.lower(), price, qty)
                        except (KeyError, ValueError, TypeError) as e:
                            log.debug("Failed to parse Binance liquidation event: %s", e)
                            continue

            except asyncio.CancelledError:
                self.connected = False
                log.info("Binance liquidation feed stopped: %s", self.symbol)
                return
            except Exception as exc:
                self.connected = False
                retry_count += 1
                if retry_count > self.max_retries:
                    log.error(
                        "Binance liquidation feed failed after %d retries (%s): %s. Giving up for now.",
                        self.max_retries,
                        self.symbol,
                        exc,
                    )
                    return
                wait = min(30.0, backoff)
                log.warning(
                    "Binance liquidation feed reconnecting in %.1fs (attempt %d/%d): %s",
                    wait,
                    retry_count,
                    self.max_retries,
                    exc,
                )
                await asyncio.sleep(wait)
                backoff *= 2

