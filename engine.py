from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
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


@dataclass
class MarketState:
    symbol: str
    trades: deque = field(default_factory=deque)  # (ts, signed_usd, usd, side, price)
    oi: deque = field(default_factory=deque)      # (ts, open_interest)
    liquidations: deque = field(default_factory=deque)  # (ts, side, usd)
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    price: float | None = None
    connected: bool = False
    last_update: float | None = None
    structure: dict[str, Any] = field(default_factory=dict)
    liquidation_map: dict[str, Any] = field(default_factory=lambda: {
        "status": "unavailable", "reason": "COINGLASS_API_KEY is not configured"
    })

    # CVD is cumulative within one process session, never a rolling-window sum.
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cvd_usd: float = 0.0
    flow_started_at: float | None = None
    continuous_since: float | None = None

    def mark_disconnected(self) -> None:
        self.connected = False
        self.continuous_since = None

    def prune(self) -> None:
        cutoff = time.time() - settings.history_seconds
        for series in (self.trades, self.oi, self.liquidations):
            while series and series[0][0] < cutoff:
                series.popleft()

    def add_trade(self, ts: float, side: str, price: float, qty: float) -> None:
        usd = price * qty
        signed = usd if side.lower() == "buy" else -usd
        if self.flow_started_at is None:
            self.flow_started_at = ts
        if self.continuous_since is None:
            self.continuous_since = ts
        self.cvd_usd += signed
        self.trades.append((ts, signed, usd, side, price))
        self.price, self.last_update = price, ts
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

    def window(self, seconds: int, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        cutoff = now - seconds
        rows = [x for x in tuple(self.trades) if cutoff < x[0] <= now]
        complete = (self.connected and self.continuous_since is not None
                    and self.continuous_since <= cutoff
                    and settings.history_seconds >= seconds)
        status = ("complete" if complete else "disconnected" if not self.connected
                  else "insufficient_retention" if settings.history_seconds < seconds
                  else "warming_up_or_gap")
        buy = sum(x[2] for x in rows if x[3].lower() == "buy")
        sell = sum(x[2] for x in rows if x[3].lower() == "sell")
        total = buy + sell
        large = [x for x in rows if x[2] >= settings.large_trade_usd]
        return {
            "status": status, "complete": complete,
            "window_seconds": seconds, "window_start": cutoff, "window_end": now,
            "continuous_since": self.continuous_since,
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

    def oi_change(self, seconds: int, now: float | None = None) -> float | None:
        now = time.time() if now is None else now
        rows = tuple(self.oi)
        if not rows or not self.connected or now - rows[-1][0] > 60:
            return None
        cutoff = now - seconds
        # Never substitute a startup sample for a full-window baseline.
        baseline = next(((ts, v) for ts, v in reversed(rows) if ts <= cutoff), None)
        if baseline is None or cutoff - baseline[0] > 60:
            return None
        return pct_change(rows[-1][1], baseline[1])

    def price_change(self, seconds: int) -> float | None:
        cutoff = time.time() - seconds
        old = next((row[4] for row in self.trades if row[0] >= cutoff), None)
        return pct_change(self.price, old)

    def liquidation_window(self, seconds: int) -> dict[str, float]:
        cutoff = time.time() - seconds
        rows = [x for x in self.liquidations if x[0] >= cutoff]
        # Bybit reports liquidation order side: Sell closes longs, Buy closes shorts.
        return {"long_usd": round(sum(x[2] for x in rows if x[1].lower()=="sell"), 2),
                "short_usd": round(sum(x[2] for x in rows if x[1].lower()=="buy"), 2)}

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        self.prune()
        windows = {k: self.window(v, now) for k, v in
                   {"1m":60,"5m":300,"15m":900,"1h":3600,"4h":14400}.items()}
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
        oic = self.oi_change(900, now)
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
            "symbol": self.symbol, "timestamp": now, "data_schema_version": "0.5.0", "price": self.price,
            "feed_status": "live" if self.connected else "reconnecting",
            "last_update_age_seconds": round(time.time()-self.last_update, 1) if self.last_update else None,
            "trade_flow": windows, "cvd_since_start_usd": round(self.cvd_usd, 2),
            "cvd_session_id": self.session_id,
            "cvd_started_at": self.flow_started_at,
            "cvd_note": "Received trades in this session; gaps are not backfilled. Use trade_flow windows and complete flags.",
            "open_interest": {"latest": self.oi[-1][1] if self.oi else None,
                              "change_5m_pct": self.oi_change(300, now), "change_15m_pct": oic,
                              "change_1h_pct": self.oi_change(3600, now),
                              "change_4h_pct": self.oi_change(14400, now)},
            "order_book": book, "liquidations_5m": liquidations_5m,
            "structure": self.structure,
            "liquidation_map": self.liquidation_map,
            "squeeze_path_assessment": squeeze_path,
            "assessment": {"long_score": long_score, "short_score": short_score,
                           "stance": stance, "evidence": signals,
                           "score_version": "0.2.0-research",
                           "warning": "这是实时状态摘要，不是自动交易信号。"},
        }


class OrderFlowEngine:
    def __init__(self) -> None:
        self.states = {s: MarketState(s) for s in settings.symbol_list}
        self.started_at = time.time()
        self.task: asyncio.Task | None = None
        self.structure_task: asyncio.Task | None = None
        self.liquidation_map_task: asyncio.Task | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run_forever())
        self.structure_task = asyncio.create_task(self._structure_loop())
        self.liquidation_map_task = asyncio.create_task(self._liquidation_map_loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        if self.structure_task:
            self.structure_task.cancel()
        if self.liquidation_map_task:
            self.liquidation_map_task.cancel()

    async def _run_forever(self) -> None:
        topics = [f"{t}.{s}" for s in self.states for t in ("publicTrade", "orderbook.50", "tickers", "allLiquidation")]
        while True:
            try:
                async with websockets.connect(settings.bybit_ws_url, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({"op":"subscribe", "args":topics}))
                    for state in self.states.values(): state.connected = True
                    try:
                        async for raw in ws:
                            self._handle(json.loads(raw))
                    finally:
                        for state in self.states.values(): state.mark_disconnected()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("WebSocket reconnecting: %s", exc)
                for state in self.states.values(): state.mark_disconnected()
                await asyncio.sleep(3)

    def _handle(self, msg: dict) -> None:
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
                try: state.liquidations.append((now, x["S"], float(x["p"])*float(x["v"])))
                except (KeyError, ValueError): pass

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
            log.warning("CoinGlass liquidation map disabled: COINGLASS_API_KEY is not configured")
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
