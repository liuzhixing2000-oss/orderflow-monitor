from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

try:
    from .config import settings
    from .research_models import build_research_models
except ImportError:  # Flat GitHub upload compatibility.
    from config import settings
    from research_models import build_research_models

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
    # One sampled trade price about every 10 seconds. This makes 4h path
    # statistics cheap enough to compute in every minute snapshot.
    price_samples: deque = field(default_factory=deque)  # (ts, price)
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    price: float | None = None
    connected: bool = False
    last_update: float | None = None
    structure: dict[str, Any] = field(default_factory=dict)

    def prune(self) -> None:
        cutoff = time.time() - settings.history_seconds
        for series in (self.trades, self.oi, self.liquidations, self.price_samples):
            while series and series[0][0] < cutoff:
                series.popleft()

    def add_trade(self, ts: float, side: str, price: float, qty: float) -> None:
        usd = price * qty
        signed = usd if side.lower() == "buy" else -usd
        self.trades.append((ts, signed, usd, side, price))
        if not self.price_samples or ts - self.price_samples[-1][0] >= 10:
            self.price_samples.append((ts, price))
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

    def price_stats(self, seconds: int) -> dict[str, Any]:
        cutoff = time.time() - seconds
        rows = [(ts, p) for ts, p in self.price_samples if ts >= cutoff]
        if self.price is not None and (not rows or rows[-1][1] != self.price):
            rows.append((time.time(), self.price))
        if len(rows) < 2:
            return {"return_pct": None, "efficiency": None, "samples": len(rows), "span_seconds": 0}
        first = rows[0][1]
        last = rows[-1][1]
        path = sum(abs(rows[i][1] - rows[i-1][1]) for i in range(1, len(rows)))
        net = abs(last - first)
        efficiency = net / path if path else 0.0
        return {
            "return_pct": round((last / first - 1) * 100, 5) if first else None,
            "efficiency": round(efficiency, 4),
            "samples": len(rows),
            "span_seconds": round(rows[-1][0] - rows[0][0], 1),
        }

    def local_structure(self) -> dict[str, Any]:
        """Fallback HTF structure built only from a genuinely warmed price path.

        Railway currently receives 403 from Bybit REST. A few dense minutes of
        websocket ticks must therefore never be labelled as a 1h/4h trend.
        """
        out: dict[str, Any] = {}
        configs = (
            ("1h", 3600, 0.05, settings.research_min_1h_span_seconds, settings.research_min_1h_samples),
            ("4h", 14400, 0.15, settings.research_min_4h_span_seconds, settings.research_min_4h_samples),
        )
        for label, seconds, min_move, required_span, required_samples in configs:
            stat = self.price_stats(seconds)
            ret = stat.get("return_pct")
            eff = stat.get("efficiency")
            span = float(stat.get("span_seconds") or 0.0)
            samples = int(stat.get("samples") or 0)
            warmed = span >= required_span and samples >= required_samples
            if not warmed:
                trend = "warmup"
            elif ret is None or eff is None:
                trend = "range"
            elif ret >= min_move and eff >= 0.18:
                trend = "up"
            elif ret <= -min_move and eff >= 0.18:
                trend = "down"
            else:
                trend = "range"
            out[label] = {
                "trend": trend,
                "eligible": warmed,
                "return_pct": ret,
                "directional_efficiency": eff,
                "samples": samples,
                "span_seconds": span,
                "required_span_seconds": required_span,
                "required_samples": required_samples,
                "source": "local_trade_path_fallback",
            }
        return out

    def book_imbalance(self, band: float) -> dict[str, Any]:
        if not self.price:
            return {"imbalance": None, "bid_usd": 0, "ask_usd": 0,
                    "coverage_complete": False, "bid_coverage_pct": 0, "ask_coverage_pct": 0}
        lo, hi = self.price * (1-band), self.price * (1+band)
        bid = sum(p*q for p, q in self.bids.items() if p >= lo)
        ask = sum(p*q for p, q in self.asks.items() if p <= hi)
        denom = bid + ask
        bid_cov = (self.price - min(self.bids)) / self.price if self.bids else 0.0
        ask_cov = (max(self.asks) - self.price) / self.price if self.asks else 0.0
        return {
            "imbalance": round((bid-ask)/denom, 4) if denom else None,
            "bid_usd": round(bid, 2), "ask_usd": round(ask, 2),
            "coverage_complete": bid_cov >= band and ask_cov >= band,
            "bid_coverage_pct": round(bid_cov * 100, 4),
            "ask_coverage_pct": round(ask_cov * 100, 4),
        }

    def oi_change(self, seconds: int) -> float | None:
        if not self.oi:
            return None
        cutoff = time.time() - seconds
        old = next((v for ts, v in self.oi if ts >= cutoff), self.oi[0][1])
        return pct_change(self.oi[-1][1], old)

    def liquidation_window(self, seconds: int) -> dict[str, float]:
        cutoff = time.time() - seconds
        rows = [x for x in self.liquidations if x[0] >= cutoff]
        # Bybit reports liquidation order side: Sell closes longs, Buy closes shorts.
        return {"long_usd": round(sum(x[2] for x in rows if x[1].lower()=="sell"), 2),
                "short_usd": round(sum(x[2] for x in rows if x[1].lower()=="buy"), 2)}

    def snapshot(self) -> dict[str, Any]:
        windows = {k: self.window(v) for k, v in {"1m":60,"5m":300,"15m":900}.items()}
        cvd = sum(x[1] for x in self.trades)
        book = {"0.1pct": self.book_imbalance(.001), "0.5pct": self.book_imbalance(.005)}
        price_stats = {k: self.price_stats(v) for k, v in {
            "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400,
        }.items()}
        signals, long_score, short_score = [], 0, 0
        s1 = self.structure.get("1h", {}).get("trend")
        s4 = self.structure.get("4h", {}).get("trend")
        for trend, weight in ((s4, 15), (s1, 10)):
            if trend == "up": long_score += weight
            elif trend == "down": short_score += weight
            else: long_score += weight//3; short_score += weight//3
        # Legacy assessment remains unchanged in meaning while the new model is researched.
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
        long_score = min(100, 25 + long_score)
        short_score = min(100, 25 + short_score)
        gap = long_score-short_score
        stance = "买方占优" if gap >= 15 else "卖方占优" if gap <= -15 else "混合/不明确"
        snapshot = {
            "symbol": self.symbol, "timestamp": time.time(), "price": self.price,
            "feed_status": "live" if self.connected else "reconnecting",
            "last_update_age_seconds": round(time.time()-self.last_update, 1) if self.last_update else None,
            "trade_flow": windows, "cvd_since_start_usd": round(cvd, 2),
            "open_interest": {"latest": self.oi[-1][1] if self.oi else None,
                              "change_5m_pct": self.oi_change(300), "change_15m_pct": oic},
            "order_book": book, "liquidations_5m": self.liquidation_window(300),
            "price_stats": price_stats,
            "structure": self.structure,
            "assessment": {"long_score": long_score, "short_score": short_score,
                           "stance": stance, "evidence": signals,
                           "score_version": "0.2.0-research",
                           "warning": "这是实时状态摘要，不是自动交易信号。"},
        }
        snapshot["models"] = build_research_models(snapshot)
        return snapshot


class OrderFlowEngine:
    def __init__(self) -> None:
        self.states = {s: MarketState(s) for s in settings.symbol_list}
        self.started_at = time.time()
        self.task: asyncio.Task | None = None
        self.rest_structure_disabled = False

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run_forever())
        asyncio.create_task(self._structure_loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()

    async def _run_forever(self) -> None:
        topics = [f"{t}.{s}" for s in self.states for t in (
            "publicTrade", f"orderbook.{settings.book_depth}", "tickers", "allLiquidation"
        )]
        while True:
            try:
                async with websockets.connect(settings.bybit_ws_url, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({"op":"subscribe", "args":topics}))
                    for state in self.states.values(): state.connected = True
                    async for raw in ws:
                        self._handle(json.loads(raw))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("WebSocket reconnecting: %s", exc)
                for state in self.states.values(): state.connected = False
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
            if self.rest_structure_disabled:
                for state in self.states.values():
                    state.structure = state.local_structure()
                await asyncio.sleep(300)
                continue

            async with httpx.AsyncClient(timeout=15) as client:
                for symbol, state in self.states.items():
                    try:
                        state.structure = await self._fetch_structure(client, symbol)
                    except Exception as exc:
                        state.structure = state.local_structure()
                        if "403" in str(exc):
                            self.rest_structure_disabled = True
                            log.warning(
                                "Bybit REST structure returned 403 on Railway; disabling repeated REST calls "
                                "and using local 1h/4h trade-path structure fallback."
                            )
                            break
                        log.warning("Structure fetch failed for %s; using local fallback: %s", symbol, exc)
            if self.rest_structure_disabled:
                for state in self.states.values():
                    state.structure = state.local_structure()
            await asyncio.sleep(300)

    async def _fetch_structure(self, client: httpx.AsyncClient, symbol: str) -> dict[str, Any]:
        out = {}
        for label, interval in (("1h", "60"), ("4h", "240")):
            r = await client.get(f"{settings.bybit_rest_url}/v5/market/kline",
                                 params={"category":"linear","symbol":symbol,"interval":interval,"limit":80})
            r.raise_for_status(); rows = r.json()["result"]["list"]
            closes = [float(x[4]) for x in reversed(rows)]
            ema20 = self._ema(closes, 20); ema50 = self._ema(closes, 50)
            trend = "up" if closes[-1] > ema20 > ema50 else "down" if closes[-1] < ema20 < ema50 else "range"
            out[label] = {"trend": trend, "close": closes[-1], "ema20": round(ema20, 6), "ema50": round(ema50, 6),
                          "source": "bybit_rest_ema"}
        return out

    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        alpha, value = 2/(period+1), values[0]
        for x in values[1:]: value = alpha*x + (1-alpha)*value
        return value


engine = OrderFlowEngine()
