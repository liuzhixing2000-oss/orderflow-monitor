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
    from .liquidation_map import assess_squeeze_path, summarize_liquidation_map
except ImportError:  # Flat GitHub upload compatibility.
    from config import settings
    from liquidation_map import assess_squeeze_path, summarize_liquidation_map

log = logging.getLogger(__name__)


def pct_change(now: float | None, old: float | None) -> float | None:
    if now is None or old in (None, 0):
        return None
    return (now / old - 1.0) * 100.0


def identify_liquidation_regime(
    price_change_15m_pct: float | None,
    oi_change_15m_pct: float | None,
    liquidations_15m: dict[str, float],
    aggregate_liquidations_1m: dict[str, float],
) -> dict[str, Any]:
    """
    Distinguish liquidation regime using 15m price change + OI + delta.
    Only returns non-null regime when conditions are clearly supported.
    
    Regimes:
    - long_liquidation_deleveraging: shorts fleeing, longs deleveraging (downside, OI down, short liq)
    - short_squeeze: prices rising, shorts liquidating (upside, OI down, short liq)
    - fresh_position_building: prices moving, OI rising, both sides liquidating (momentum)
    """
    regime = None
    confidence = 0.0
    evidence = []
    
    if price_change_15m_pct is None or oi_change_15m_pct is None:
        return {
            "regime": None,
            "confidence": 0.0,
            "evidence": [],
            "source_data": {
                "price_change_15m_pct": price_change_15m_pct,
                "oi_change_15m_pct": oi_change_15m_pct,
                "liquidations_15m": liquidations_15m,
                "aggregate_liquidations_1m": aggregate_liquidations_1m,
            },
        }
    
    short_liq_15m = liquidations_15m.get("short_usd", 0.0)
    long_liq_15m = liquidations_15m.get("long_usd", 0.0)
    short_liq_1m = aggregate_liquidations_1m.get("short_usd", 0.0)
    long_liq_1m = aggregate_liquidations_1m.get("long_usd", 0.0)
    
    # Long liquidation deleveraging: downside move + OI decline + short liquidations
    if price_change_15m_pct < -0.5 and oi_change_15m_pct < -0.10 and short_liq_15m > 0:
        regime = "long_liquidation_deleveraging"
        confidence = min(0.95, 0.30 + abs(price_change_15m_pct) * 0.15 + abs(oi_change_15m_pct) * 0.20)
        evidence.append(f"价格下跌 {price_change_15m_pct:.2f}% (空方获利/多杀多)")
        evidence.append(f"OI下降 {oi_change_15m_pct:.2f}% (头寸出清)")
        evidence.append(f"空单清算 ${short_liq_15m:,.0f} (被迫平仓)")
    
    # Short squeeze: upside move + OI decline + short liquidations
    elif price_change_15m_pct > 0.5 and oi_change_15m_pct < -0.10 and short_liq_15m > 0:
        regime = "short_squeeze"
        confidence = min(0.95, 0.30 + abs(price_change_15m_pct) * 0.15 + abs(oi_change_15m_pct) * 0.20)
        evidence.append(f"价格上升 {price_change_15m_pct:.2f}% (逼空)")
        evidence.append(f"OI下降 {oi_change_15m_pct:.2f}% (头寸出清)")
        evidence.append(f"空单清算 ${short_liq_15m:,.0f} (被迫平仓)")
    
    # Fresh position building: directional move + OI rise + both sides liquidating
    elif abs(price_change_15m_pct) > 1.0 and oi_change_15m_pct > 0.10 and (short_liq_15m > 0 or long_liq_15m > 0):
        regime = "fresh_position_building"
        confidence = min(0.90, 0.25 + abs(price_change_15m_pct) * 0.12 + oi_change_15m_pct * 0.15)
        direction = "上" if price_change_15m_pct > 0 else "下"
        evidence.append(f"价格{direction}行 {abs(price_change_15m_pct):.2f}% (趋势动力)")
        evidence.append(f"OI增加 {oi_change_15m_pct:.2f}% (新头寸涌入)")
        if short_liq_15m > 0 and long_liq_15m > 0:
            evidence.append(f"双向清算: 空单${short_liq_15m:,.0f} + 多单${long_liq_15m:,.0f}")
        elif short_liq_15m > 0:
            evidence.append(f"空单清算 ${short_liq_15m:,.0f}")
        else:
            evidence.append(f"多单清算 ${long_liq_15m:,.0f}")
    
    return {
        "regime": regime,
        "confidence": round(confidence, 3) if regime else 0.0,
        "evidence": evidence,
        "source_data": {
            "price_change_15m_pct": round(price_change_15m_pct, 3),
            "oi_change_15m_pct": round(oi_change_15m_pct, 3),
            "liquidations_15m": liquidations_15m,
            "aggregate_liquidations_1m": aggregate_liquidations_1m,
        },
    }


@dataclass
class MarketState:
    symbol: str
    trades: deque = field(default_factory=deque)  # (ts, signed_usd, usd, side, price)
    oi: deque = field(default_factory=deque)      # (ts, open_interest)
    # Multi-exchange liquidations: {exchange: [(ts, side, usd), ...]}
    liquidations_by_exchange: dict[str, deque] = field(default_factory=lambda: {
        "bybit": deque(), "binance": deque()
    })
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    price: float | None = None
    connected: bool = False
    last_update: float | None = None
    structure: dict[str, Any] = field(default_factory=dict)
    liquidation_map: dict[str, Any] = field(default_factory=lambda: {
        "status": "unavailable", "reason": "COINGLASS_API_KEY is not configured"
    })
    binance_connected: bool = False

    def prune(self) -> None:
        cutoff = time.time() - settings.history_seconds
        for series in (self.trades, self.oi):
            while series and series[0][0] < cutoff:
                series.popleft()
        for exchange_liq in self.liquidations_by_exchange.values():
            while exchange_liq and exchange_liq[0][0] < cutoff:
                exchange_liq.popleft()

    def add_trade(self, ts: float, side: str, price: float, qty: float) -> None:
        usd = price * qty
        signed = usd if side.lower() == "buy" else -usd
        self.trades.append((ts, signed, usd, side, price))
        self.price, self.last_update = price, ts
        self.prune()

    def add_liquidation(self, exchange: str, ts: float, side: str, usd: float) -> None:
        """Add liquidation event with exchange tag."""
        if exchange not in self.liquidations_by_exchange:
            self.liquidations_by_exchange[exchange] = deque()
        self.liquidations_by_exchange[exchange].append((ts, side, usd))
        self.prune()

    def update_book(self, msg_type: str, bids: list, asks: list) -> None:
        if msg_type == "snapshot":
            self.bids.clear(); self.asks.clear()
        for target, rows in ((self.bids, bids), (self.asks, asks)):
            for p, q in rows:
                price, qty = float(p), float(q)
                if qty == 0:
                    target.pop(price, None)
                else:
                    target[price] = qty

    def window(self, seconds: int) -> dict[str, Any]:
        cutoff = time.time() - seconds
        rows = [x for x in self.trades if x[0] >= cutoff]
        buy = sum(x[2] for x in rows if x[3].lower() == "buy")
        sell = sum(x[2] for x in rows if x[3].lower() == "sell")
        total = buy + sell
        large = [x for x in rows if x[2] >= settings.large_trade_usd]
        return {
            "buy_usd": round(buy, 2), "sell_usd": round(sell, 2),
            "delta_usd": round(buy - sell, 2),
            "buy_ratio": round(buy / total, 4) if total else None,
            "trade_count": len(rows), "large_buy_count": sum(x[3].lower() == "buy" for x in large),
            "large_sell_count": sum(x[3].lower() == "sell" for x in large),
        }

    def book_imbalance(self, band: float) -> dict[str, Any]:
        if not self.price:
            return {"imbalance": None, "bid_usd": 0, "ask_usd": 0}
        lo, hi = self.price * (1-band), self.price * (1+band)
        bid = sum(p*q for p, q in self.bids.items() if p >= lo)
        ask = sum(p*q for p, q in self.asks.items() if p <= hi)
        denom = bid + ask
        return {"imbalance": round((bid-ask)/denom, 4) if denom else None,
                "bid_usd": round(bid, 2), "ask_usd": round(ask, 2)}

    def oi_change(self, seconds: int) -> float | None:
        if not self.oi:
            return None
        cutoff = time.time() - seconds
        old = next((v for ts, v in self.oi if ts >= cutoff), self.oi[0][1])
        return pct_change(self.oi[-1][1], old)

    def price_change(self, seconds: int) -> float | None:
        cutoff = time.time() - seconds
        old = next((row[4] for row in self.trades if row[0] >= cutoff), None)
        return pct_change(self.price, old)

    def _liquidation_window_per_exchange(self, seconds: int) -> dict[str, dict[str, float]]:
        """Return liquidations aggregated by exchange."""
        cutoff = time.time() - seconds
        result = {}
        for exchange, liq_deque in self.liquidations_by_exchange.items():
            rows = [x for x in liq_deque if x[0] >= cutoff]
            # Bybit/Binance: Sell closes longs, Buy closes shorts.
            result[exchange] = {
                "long_usd": round(sum(x[2] for x in rows if x[1].lower() == "sell"), 2),
                "short_usd": round(sum(x[2] for x in rows if x[1].lower() == "buy"), 2),
            }
        return result

    def liquidation_window(self, seconds: int) -> dict[str, float]:
        """Return aggregated liquidations across all exchanges."""
        per_exchange = self._liquidation_window_per_exchange(seconds)
        total_long = sum(data["long_usd"] for data in per_exchange.values())
        total_short = sum(data["short_usd"] for data in per_exchange.values())
        return {"long_usd": round(total_long, 2), "short_usd": round(total_short, 2)}

    def snapshot(self) -> dict[str, Any]:
        windows = {k: self.window(v) for k, v in {"1m": 60, "5m": 300, "15m": 900}.items()}
        cvd = sum(x[1] for x in self.trades)
        book = {"0.1pct": self.book_imbalance(.001), "0.5pct": self.book_imbalance(.005)}
        signals, long_score, short_score = [], 0, 0
        s1 = self.structure.get("1h", {}).get("trend")
        s4 = self.structure.get("4h", {}).get("trend")
        for trend, weight in ((s4, 15), (s1, 10)):
            if trend == "up": long_score += weight
            elif trend == "down": short_score += weight
            else: long_score += weight//3; short_score += weight//3
        # Multi-window trade flow: recent activity matters, but agreement matters more.
        for key, weight in (("1m", 7), ("5m", 10), ("15m", 8)):
            ratio = windows[key]["buy_ratio"]
            if ratio is None: continue
            strength = min(1.0, abs(ratio-.5)/.18)
            points = round(weight*strength)
            if ratio > .5: long_score += points
            elif ratio < .5: short_score += points
        d5 = windows["5m"]["delta_usd"]
        if d5 > 0: signals.append("5分钟主动买盘占优")
        elif d5 < 0: signals.append("5分钟主动卖盘占优")
        imb = book["0.1pct"]["imbalance"]
        if imb is not None and imb > .12: signals.append("近端买盘深度占优"); long_score += min(5, round(abs(imb)*10))
        elif imb is not None and imb < -.12: signals.append("近端卖盘深度占优"); short_score += min(5, round(abs(imb)*10))
        oic = self.oi_change(900)
        if oic is not None and abs(oic) >= .15:
            signals.append(f"15分钟OI{'增加' if oic>0 else '下降'} {abs(oic):.2f}%")
            if oic > 0:
                if d5 > 0: long_score += 10
                elif d5 < 0: short_score += 10
        # Baseline points prevent absent/noisy features from looking like extreme conviction.
        long_score = min(100, 25 + long_score)
        short_score = min(100, 25 + short_score)
        gap = long_score-short_score
        stance = "买方占优" if gap >= 15 else "卖方占优" if gap <= -15 else "混合/不明确"
        liquidations_5m = self.liquidation_window(300)
        liquidations_15m = self.liquidation_window(900)
        
        # Liquidation regime detection
        liquidations_1m = self.liquidation_window(60)
        regime_info = identify_liquidation_regime(
            price_change_15m_pct=self.price_change(900),
            oi_change_15m_pct=oic,
            liquidations_15m=liquidations_15m,
            aggregate_liquidations_1m=liquidations_1m,
        )
        
        # Per-exchange breakdown
        per_exchange_5m = self._liquidation_window_per_exchange(300)
        
        squeeze_path = assess_squeeze_path(
            self.liquidation_map,
            trade_flow=windows,
            structure=self.structure,
            order_book=book,
            oi_change_15m=oic,
            liquidations_5m=liquidations_5m,
            price_change_5m_pct=self.price_change(300),
        )
        return {
            "symbol": self.symbol, "timestamp": time.time(), "price": self.price,
            "feed_status": "live" if self.connected else "reconnecting",
            "binance_status": "live" if self.binance_connected else "disconnected",
            "last_update_age_seconds": round(time.time()-self.last_update, 1) if self.last_update else None,
            "trade_flow": windows, "cvd_since_start_usd": round(cvd, 2),
            "open_interest": {"latest": self.oi[-1][1] if self.oi else None,
                              "change_5m_pct": self.oi_change(300), "change_15m_pct": oic},
            "order_book": book, 
            # Enhanced liquidation snapshot
            "liquidations_1m": liquidations_1m,
            "liquidations_5m": liquidations_5m,
            "liquidations_15m": liquidations_15m,
            "liquidations_by_exchange": {
                "exchange_breakdown_5m": per_exchange_5m,
                "bybit": {
                    "long_liquidations_5m_usd": per_exchange_5m.get("bybit", {}).get("long_usd", 0.0),
                    "short_liquidations_5m_usd": per_exchange_5m.get("bybit", {}).get("short_usd", 0.0),
                },
                "binance": {
                    "long_liquidations_5m_usd": per_exchange_5m.get("binance", {}).get("long_usd", 0.0),
                    "short_liquidations_5m_usd": per_exchange_5m.get("binance", {}).get("short_usd", 0.0),
                },
            },
            "liquidation_regime": regime_info,
            "structure": self.structure,
            "liquidation_map": self.liquidation_map,
            "squeeze_path_assessment": squeeze_path,
            "assessment": {"long_score": long_score, "short_score": short_score,
                           "stance": stance, "evidence": signals,
                           "score_version": "0.3.0-liquidation-regime",
                           "warning": "这是实时状态摘要，不是自动交易信号。"},
        }


class OrderFlowEngine:
    def __init__(self) -> None:
        self.states = {s: MarketState(s) for s in settings.symbol_list}
        self.started_at = time.time()
        self.task: asyncio.Task | None = None
        self.structure_task: asyncio.Task | None = None
        self.liquidation_map_task: asyncio.Task | None = None
        self.binance_task: asyncio.Task | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run_forever())
        self.structure_task = asyncio.create_task(self._structure_loop())
        self.liquidation_map_task = asyncio.create_task(self._liquidation_map_loop())
        self.binance_task = asyncio.create_task(self._binance_liquidation_loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        if self.structure_task:
            self.structure_task.cancel()
        if self.liquidation_map_task:
            self.liquidation_map_task.cancel()
        if self.binance_task:
            self.binance_task.cancel()

    async def _run_forever(self) -> None:
        """Bybit WebSocket: publicTrade, orderbook, tickers, allLiquidation"""
        topics = [f"{t}.{s}" for s in self.states for t in ("publicTrade", "orderbook.50", "tickers", "allLiquidation")]
        while True:
            try:
                async with websockets.connect(settings.bybit_ws_url, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({"op":"subscribe", "args":topics}))
                    for state in self.states.values(): state.connected = True
                    async for raw in ws:
                        self._handle_bybit(json.loads(raw))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("Bybit WebSocket reconnecting: %s", exc)
                for state in self.states.values(): state.connected = False
                await asyncio.sleep(3)

    def _handle_bybit(self, msg: dict) -> None:
        topic = msg.get("topic", "")
        symbol = next((s for s in self.states if topic.endswith(s)), None)
        if not symbol: return
        state, data = self.states[symbol], msg.get("data", {})
        now = time.time()
        if topic.startswith("publicTrade"):
            for t in data:
                state.add_trade(float(t.get("T", now*1000))/1000, t["S"], float(t["p"]), float(t["v"]))
        elif topic.startswith("orderbook"):
            state.update_book(msg.get("type", "delta"), data.get("b", []), data.get("a", []))
        elif topic.startswith("tickers"):
            if data.get("lastPrice"): state.price = float(data["lastPrice"])
            if data.get("openInterest"):
                state.oi.append((now, float(data["openInterest"])))
            state.last_update = now; state.prune()
        elif topic.startswith("allLiquidation"):
            for x in (data if isinstance(data, list) else [data]):
                try: 
                    state.add_liquidation("bybit", now, x["S"], float(x["p"])*float(x["v"]))
                except (KeyError, ValueError): pass

    async def _binance_liquidation_loop(self) -> None:
        """Free public Binance USD-M forceOrder stream with reconnect/failure isolation."""
        while True:
            try:
                async with websockets.connect("wss://fstream.binance.com/stream", ping_interval=20, ping_timeout=20) as ws:
                    # Subscribe to all configured symbols' forceOrder streams
                    symbols_lower = [s.replace("USDT", "").lower() for s in self.states.keys()]
                    streams = [f"{sym}@forceOrder" for sym in symbols_lower]
                    await ws.send(json.dumps({
                        "method": "SUBSCRIBE",
                        "params": streams,
                        "id": 1
                    }))
                    for state in self.states.values():
                        state.binance_connected = True
                    log.info("Binance USD-M forceOrder connected for symbols: %s", ", ".join(self.states.keys()))
                    
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            self._handle_binance(msg)
                        except Exception as exc:
                            log.warning("Binance message parse error: %s", exc)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("Binance forceOrder reconnecting: %s", exc)
                for state in self.states.values():
                    state.binance_connected = False
                await asyncio.sleep(5)

    def _handle_binance(self, msg: dict) -> None:
        """Process Binance forceOrder stream: {e: 'forceOrder', E: timestamp, o: {s: symbol, S: side, q: qty, p: price, ...}}"""
        if msg.get("e") != "forceOrder":
            return
        
        data = msg.get("o", {})
        symbol_raw = data.get("s", "").upper()
        
        # Map Binance symbol (BTCUSDT) to our symbol list
        symbol = None
        for s in self.states.keys():
            if s == symbol_raw:
                symbol = s
                break
        
        if not symbol:
            return
        
        try:
            side = data.get("S", "").upper()  # BUY or SELL
            qty = float(data.get("q", 0))
            price = float(data.get("p", 0))
            
            if qty > 0 and price > 0:
                usd_amount = qty * price
                # Binance forceOrder: SELL closes longs, BUY closes shorts
                self.states[symbol].add_liquidation("binance", time.time(), side, usd_amount)
        except (ValueError, KeyError) as exc:
            log.debug("Binance forceOrder parse error for %s: %s", symbol_raw, exc)

    async def _structure_loop(self) -> None:
        while True:
            async with httpx.AsyncClient(timeout=15) as client:
                for symbol, state in self.states.items():
                    try:
                        state.structure = await self._fetch_structure(client, symbol)
                    except Exception as exc:
                        log.warning("Structure fetch failed for %s: %s", symbol, exc)
            await asyncio.sleep(300)

    async def _liquidation_map_loop(self) -> None:
        if not settings.coinglass_api_key:
            log.info("CoinGlass liquidation map disabled: COINGLASS_API_KEY is not configured (optional)")
            return
        while True:
            if not any(state.price for state in self.states.values()):
                await asyncio.sleep(5)
                continue
            async with httpx.AsyncClient(timeout=20) as client:
                for symbol, state in self.states.items():
                    if not state.price:
                        continue
                    try:
                        coin = symbol.removesuffix("USDT")
                        response = await client.get(
                            f"{settings.coinglass_api_url}/api/futures/liquidation/aggregated-map",
                            params={"symbol": coin, "range": settings.liquidation_map_range},
                            headers={"CG-API-KEY": settings.coinglass_api_key},
                        )
                        response.raise_for_status()
                        state.liquidation_map = summarize_liquidation_map(
                            response.json(),
                            state.price,
                            max_distance_pct=settings.liquidation_map_max_distance_pct,
                            cluster_band_pct=settings.liquidation_map_cluster_band_pct,
                        )
                    except Exception as exc:
                        previous = state.liquidation_map
                        state.liquidation_map = {
                            "status": "stale" if previous.get("status") == "live" else "unavailable",
                            "reason": str(exc),
                            "last_good": previous if previous.get("status") == "live" else None,
                        }
                        log.warning("Liquidation map fetch failed for %s: %s", symbol, exc)
            await asyncio.sleep(settings.liquidation_map_refresh_seconds)

    async def _fetch_structure(self, client: httpx.AsyncClient, symbol: str) -> dict[str, Any]:
        out = {}
        for label, interval in (("1h", "60"), ("4h", "240")):
            r = await client.get(f"{settings.bybit_rest_url}/v5/market/kline",
                                 params={"category":"linear","symbol":symbol,"interval":interval,"limit":80})
            r.raise_for_status(); rows = r.json()["result"]["list"]
            closes = [float(x[4]) for x in reversed(rows)]
            ema20 = self._ema(closes, 20); ema50 = self._ema(closes, 50)
            trend = "up" if closes[-1] > ema20 > ema50 else "down" if closes[-1] < ema20 < ema50 else "range"
            out[label] = {"trend": trend, "close": closes[-1], "ema20": round(ema20, 6), "ema50": round(ema50, 6)}
        return out

    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        alpha, value = 2/(period+1), values[0]
        for x in values[1:]: value = alpha*x + (1-alpha)*value
        return value


engine = OrderFlowEngine()

