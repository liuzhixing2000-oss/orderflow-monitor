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

log = logging.getLogger(__name__)


@dataclass
class LiquidationAggregator:
    """Aggregate realized liquidations from multiple exchanges over time windows."""
    symbol: str
    exchanges: dict[str, deque] = field(default_factory=lambda: {
        "bybit": deque(),
        "binance": deque(),
    })
    
    def add_liquidation(self, exchange: str, ts: float, side: str, usd: float) -> None:
        """Record a liquidation event. Side: 'long' (sell-to-close) or 'short' (buy-to-close)."""
        if exchange not in self.exchanges:
            self.exchanges[exchange] = deque()
        self.exchanges[exchange].append((ts, side.lower(), usd))
    
    def window(self, seconds: int) -> dict[str, Any]:
        """Aggregate liquidations over a time window."""
        cutoff = time.time() - seconds
        result = {
            "total_long_usd": 0.0,
            "total_short_usd": 0.0,
            "by_exchange": {},
        }
        for exchange, events in self.exchanges.items():
            long = short = 0.0
            for ts, side, usd in events:
                if ts >= cutoff:
                    if side == "long":
                        long += usd
                    else:
                        short += usd
            result["by_exchange"][exchange] = {
                "long_usd": round(long, 2),
                "short_usd": round(short, 2),
            }
            result["total_long_usd"] += long
            result["total_short_usd"] += short
        
        result["total_long_usd"] = round(result["total_long_usd"], 2)
        result["total_short_usd"] = round(result["total_short_usd"], 2)
        return result
    
    def prune(self, history_seconds: int) -> None:
        """Remove old events beyond history window."""
        cutoff = time.time() - history_seconds
        for events in self.exchanges.values():
            while events and events[0][0] < cutoff:
                events.popleft()


class BinanceUSDMLiquidationStream:
    """
    Isolated Binance USD-M forced liquidation stream (free public WebSocket).
    Failure does NOT break the main engine; it operates independently.
    """
    def __init__(self, symbols: list[str], callback):
        self.symbols = symbols
        self.callback = callback  # fn(symbol, side, usd)
        self.task: asyncio.Task | None = None
        self.connected = False
        
    async def start(self) -> None:
        self.task = asyncio.create_task(self._run())
        log.info("BinanceUSDMLiquidationStream: start requested for %s", self.symbols)
    
    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        self.connected = False
    
    async def _run(self) -> None:
        """Connect to Binance USD-M forceOrder stream."""
        # Binance symbol format: BTCUSDT -> btcusdt (lowercase)
        streams = [f"{s.lower()}@forceOrder" for s in self.symbols]
        stream_path = "/".join(streams)
        ws_url = f"wss://fstream.binance.com/stream?streams={stream_path}"
        
        while True:
            try:
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                    self.connected = True
                    log.info("BinanceUSDMLiquidationStream: connected")
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            data = msg.get("data", {})
                            
                            # Binance forceOrder format:
                            # {
                            #   "e": "forceOrder",
                            #   "E": 1234567890,
                            #   "o": {
                            #     "s": "BTCUSDT",
                            #     "S": "BUY",  // direction of forced liquidation
                            #     "o": "MARKET",
                            #     "f": "GTC",
                            #     "q": "0.001",  // qty
                            #     "p": "45123.67",  // price
                            #     "ap": "45123.67",  // avg price
                            #     "X": "FILLED",
                            #     "l": "0.001",
                            #     "z": "0.001",
                            #     "T": 1234567890,
                            #   }
                            # }
                            
                            if data.get("e") != "forceOrder":
                                continue
                            
                            order = data.get("o", {})
                            symbol = order.get("s", "").upper()
                            direction = order.get("S", "").upper()
                            qty = float(order.get("q", 0))
                            price = float(order.get("p", 0))
                            
                            if not symbol or qty <= 0 or price <= 0:
                                continue
                            
                            usd = qty * price
                            # BUY = closing shorts, SELL = closing longs
                            side = "short" if direction == "BUY" else "long"
                            
                            self.callback(symbol, side, usd)
                        except (json.JSONDecodeError, KeyError, ValueError) as e:
                            log.debug("BinanceUSDMLiquidationStream: parse error: %s", e)
                            continue
            except asyncio.CancelledError:
                self.connected = False
                return
            except Exception as exc:
                self.connected = False
                log.debug("BinanceUSDMLiquidationStream: reconnecting after %s", exc)
                await asyncio.sleep(3)


def classify_liquidation_regime(
    price_change_15m_pct: float | None,
    oi_change_15m_pct: float | None,
    trade_delta_15m_usd: float,
    liquidations_15m: dict[str, float],
) -> dict[str, Any]:
    """
    Classify liquidation regime based on 15m window: price change, OI change, trade delta.
    
    Regimes:
    - long_deleveraging: longs liquidated + OI down 10%+ + price down
    - short_squeeze: shorts liquidated + price up
    - fresh_position_building: shorts liquidated + OI up + positive delta
    - uncertain: insufficient evidence
    """
    long_liq = liquidations_15m.get("long_usd", 0.0)
    short_liq = liquidations_15m.get("short_usd", 0.0)
    
    regime = "uncertain"
    confidence = 0
    evidence = []
    
    # Check for long deleveraging: longs liquidating + OI contracting + price down
    if long_liq > 0:
        evidence.append(f"Long liquidations: ${long_liq:,.0f}")
        if price_change_15m_pct is not None and price_change_15m_pct < -0.5:
            evidence.append(f"Price down {price_change_15m_pct:.2f}%")
            if oi_change_15m_pct is not None and oi_change_15m_pct < -10.0:
                evidence.append(f"OI down {oi_change_15m_pct:.2f}%")
                regime = "long_deleveraging"
                confidence = min(100, 70 + int(abs(oi_change_15m_pct) / 10))
    
    # Check for short squeeze: shorts liquidating + price up
    if short_liq > 0:
        evidence.append(f"Short liquidations: ${short_liq:,.0f}")
        if price_change_15m_pct is not None and price_change_15m_pct > 0.5:
            evidence.append(f"Price up {price_change_15m_pct:.2f}%")
            # Distinguish squeeze from fresh position building
            if (oi_change_15m_pct is not None and oi_change_15m_pct > 5.0 and
                trade_delta_15m_usd > 0):
                evidence.append(f"OI up {oi_change_15m_pct:.2f}%, delta positive")
                regime = "fresh_position_building"
                confidence = min(100, 60 + int(oi_change_15m_pct / 5))
            elif oi_change_15m_pct is None or oi_change_15m_pct <= 5.0:
                regime = "short_squeeze"
                confidence = min(100, 70 + int(abs(price_change_15m_pct) / 0.5))
    
    return {
        "regime": regime,
        "confidence": confidence,
        "evidence": evidence,
    }

