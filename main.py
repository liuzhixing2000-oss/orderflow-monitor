from contextlib import asynccontextmanager
import asyncio
import json
import logging
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from mcp.server.fastmcp import FastMCP

try:
    from .config import settings
    from .engine import engine
    from .storage import storage
    from .sample_b import SampleB
except ImportError:  # Flat GitHub upload compatibility.
    from config import settings
    from engine import engine
    from storage import storage
    from sample_b import SampleB


APP_VERSION = "0.3.4-research"
sample_b = SampleB(storage)
log = logging.getLogger("orderflow.snapshot")


def normalize_symbol(symbol: str) -> str:
    normalized = symbol.upper().replace("/", "").replace("-", "")
    if not normalized.endswith("USDT"):
        normalized += "USDT"
    return normalized


def get_snapshot(symbol: str) -> dict:
    normalized = normalize_symbol(symbol)
    state = engine.states.get(normalized)
    if not state:
        raise ValueError(f"Unsupported symbol. Supported: {', '.join(settings.symbol_list)}")
    return state.snapshot()


def research_status_payload() -> dict:
    return {
        "sample_b": sample_b.status(),
        "database": storage.path,
        "observations": storage.status(),
        "price_path": storage.price_path_status(),
        "price_path_retention_days": settings.research_price_path_retention_days,
        "independent_events": storage.event_status(),
        "materialized_outcomes": storage.outcome_status(),
        "event_thresholds": settings.research_threshold_list,
        "event_cooldown_minutes": settings.research_event_cooldown_minutes,
        "warmup": {
            "min_1h_span_seconds": settings.research_min_1h_span_seconds,
            "min_4h_span_seconds": settings.research_min_4h_span_seconds,
            "min_1h_samples": settings.research_min_1h_samples,
            "min_4h_samples": settings.research_min_4h_samples,
        },
        "note": "Only clock-time-qualified threshold crossings are eligible. Matured outcomes are materialized from the 10-second path and retained after raw path pruning.",
    }


# Read-only MCP tools for ChatGPT. This server cannot place, edit or cancel orders.
mcp = FastMCP(
    "BTC ETH SOL Order Flow Monitor",
    instructions=(
        "Read-only live Bybit order-flow research data. Use snapshots as decision "
        "support, never claim that a score is a validated trading signal."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)


@mcp.tool()
def get_market_snapshot(symbol: str) -> dict:
    """Return the latest live order-flow snapshot for BTC, ETH or SOL."""
    return get_snapshot(symbol)


@mcp.tool()
def get_all_market_snapshots() -> dict:
    """Return current live order-flow snapshots for BTC, ETH and SOL."""
    return {symbol: state.snapshot() for symbol, state in engine.states.items()}


@mcp.tool()
def get_research_status() -> dict:
    """Return observation, price-path, event and materialized-outcome counts."""
    return research_status_payload()


@mcp.tool()
def get_score_bucket_results(symbol: str, horizon_minutes: int = 60, side: str = "long") -> dict:
    """Return legacy minute-observation forward returns grouped by score band."""
    normalized = normalize_symbol(symbol)
    if normalized not in engine.states:
        raise ValueError(f"Unsupported symbol. Supported: {', '.join(settings.symbol_list)}")
    rows = storage.buckets(normalized, horizon_minutes, side.lower())
    return {
        "symbol": normalized,
        "side": side.lower(),
        "horizon_minutes": horizon_minutes,
        "round_trip_cost_pct": settings.round_trip_cost_pct,
        "buckets": rows,
        "warning": "Overlapping minute observations are correlated; use threshold-event results for formal research.",
    }


@mcp.tool()
def get_threshold_event_results(symbol: str, horizon_minutes: int = 60,
                                side: str = "long", threshold: int = 80) -> dict:
    """Return materialized independent continuation threshold-event results after costs."""
    normalized = normalize_symbol(symbol)
    if normalized not in engine.states:
        raise ValueError(f"Unsupported symbol. Supported: {', '.join(settings.symbol_list)}")
    return storage.event_results(normalized, horizon_minutes, side.lower(), threshold)


@mcp.tool()
def get_threshold_sweep_results(symbol: str, horizon_minutes: int = 60,
                                side: str = "long") -> dict:
    """Compare 50/60/70/80/90 continuation threshold-crossing expectancy."""
    normalized = normalize_symbol(symbol)
    if normalized not in engine.states:
        raise ValueError(f"Unsupported symbol. Supported: {', '.join(settings.symbol_list)}")
    return {
        "symbol": normalized,
        "side": side.lower(),
        "horizon_minutes": horizon_minutes,
        "thresholds": storage.event_threshold_sweep(normalized, horizon_minutes, side.lower()),
        "warning": "Do not infer an edge until sample counts and out-of-sample stability are adequate.",
    }


mcp_http_app = mcp.streamable_http_app()


async def snapshot_sampler():
    while True:
        for state in engine.states.values():
            snapshot = state.snapshot()
            created_events = await asyncio.to_thread(storage.insert, snapshot)
            await asyncio.to_thread(sample_b.observe, snapshot)
            # Public market data only. This lets the connected Railway app provide
            # the latest snapshot when direct HTTP/MCP access is unavailable.
            log.info("ORDERFLOW_SNAPSHOT %s", json.dumps(snapshot, separators=(",", ":")))
            for event in created_events:
                log.info("RESEARCH_EVENT %s", json.dumps(event, separators=(",", ":")))
        await asyncio.sleep(settings.snapshot_interval_seconds)


async def price_path_sampler():
    while True:
        ts = time.time()
        points = [
            (ts, symbol, float(state.price))
            for symbol, state in engine.states.items()
            if state.price is not None and state.connected
            and state.last_update is not None and 0 <= ts - state.last_update <= 5
        ]
        if points:
            await asyncio.to_thread(storage.insert_price_points, points)
        await asyncio.sleep(settings.research_price_path_interval_seconds)


async def outcome_sampler():
    """Persist matured event outcomes before old 10-second path rows are pruned."""
    while True:
        result = await asyncio.to_thread(storage.evaluate_pending_outcomes)
        await asyncio.to_thread(sample_b.evaluate)
        if result.get("materialized"):
            log.info("RESEARCH_OUTCOMES %s", json.dumps(result, separators=(",", ":")))
        await asyncio.sleep(60)


def restore_price_history() -> int:
    history = storage.load_price_history(14_400)
    restored = 0
    for symbol, points in history.items():
        state = engine.states.get(symbol)
        if not state or not points:
            continue
        state.price_samples.extend(points)
        state.price = points[-1][1]
        restored += len(points)
    return restored


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init()
    if settings.sample_b_enabled:
        sample_b.init()
        log.info("SAMPLE_B_STATUS %s", json.dumps(sample_b.status(), separators=(",", ":")))
    restored = await asyncio.to_thread(restore_price_history)
    if restored:
        log.info("RESEARCH_HISTORY_RESTORED points=%d", restored)
    await engine.start()
    sampler = asyncio.create_task(snapshot_sampler())
    path_sampler = asyncio.create_task(price_path_sampler())
    outcome_task = asyncio.create_task(outcome_sampler())
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        sampler.cancel()
        path_sampler.cancel()
        outcome_task.cancel()
        await engine.stop()


app = FastAPI(title="Crypto Order Flow Monitor", version=APP_VERSION, lifespan=lifespan)


def authorize(x_api_key: str | None = Header(default=None)) -> None:
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/health")
def health():
    return {
        "ok": True,
        "version": APP_VERSION,
        "symbols": settings.symbol_list,
        "feeds": {s: x.connected for s, x in engine.states.items()},
        "mcp": "/mcp/",
        "research_event_thresholds": settings.research_threshold_list,
        "price_path_retention_days": settings.research_price_path_retention_days,
    }


@app.get("/symbols", dependencies=[Depends(authorize)])
def symbols():
    return {"symbols": settings.symbol_list}


@app.get("/snapshot/{symbol}", dependencies=[Depends(authorize)])
def snapshot(symbol: str):
    try:
        return get_snapshot(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/snapshot", dependencies=[Depends(authorize)])
def all_snapshots():
    return {s: state.snapshot() for s, state in engine.states.items()}


@app.get("/research/status", dependencies=[Depends(authorize)])
def research_status():
    return research_status_payload()


@app.get("/research/score-buckets/{symbol}", dependencies=[Depends(authorize)])
def score_buckets(symbol: str, horizon: int = 60, side: str = "long"):
    normalized = normalize_symbol(symbol)
    if normalized not in engine.states:
        raise HTTPException(status_code=404, detail="Unsupported symbol")
    try:
        rows = storage.buckets(normalized, horizon, side.lower())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "symbol": normalized,
        "side": side.lower(),
        "horizon_minutes": horizon,
        "round_trip_cost_pct": settings.round_trip_cost_pct,
        "buckets": rows,
        "warning": "连续分钟样本高度相关；正式验证请使用 /research/events 或 threshold-sweep。",
    }


@app.get("/research/events/{symbol}", dependencies=[Depends(authorize)])
def research_events(symbol: str, horizon: int = 60, side: str = "long", threshold: int = 80):
    normalized = normalize_symbol(symbol)
    if normalized not in engine.states:
        raise HTTPException(status_code=404, detail="Unsupported symbol")
    try:
        return storage.event_results(normalized, horizon, side.lower(), threshold)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/research/threshold-sweep/{symbol}", dependencies=[Depends(authorize)])
def threshold_sweep(symbol: str, horizon: int = 60, side: str = "long"):
    normalized = normalize_symbol(symbol)
    if normalized not in engine.states:
        raise HTTPException(status_code=404, detail="Unsupported symbol")
    try:
        rows = storage.event_threshold_sweep(normalized, horizon, side.lower())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "symbol": normalized,
        "side": side.lower(),
        "horizon_minutes": horizon,
        "thresholds": rows,
        "warning": "仅当样本数、周度稳定性和样本外结果都足够时，才考虑是否存在可交易优势。",
    }


@app.get("/research/sample-b/status", dependencies=[Depends(authorize)])
def sample_b_status():
    return sample_b.status()


@app.get("/research/sample-b/export", dependencies=[Depends(authorize)])
def sample_b_export(after_id: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=1000)):
    return sample_b.export(after_id, limit)


app.mount("/mcp", mcp_http_app)
