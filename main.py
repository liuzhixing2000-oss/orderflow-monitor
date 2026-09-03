from contextlib import asynccontextmanager
import asyncio

from fastapi import Depends, FastAPI, Header, HTTPException
from mcp.server.fastmcp import FastMCP

try:
    from .config import settings
    from .engine import engine
    from .storage import storage
except ImportError:  # Flat GitHub upload compatibility.
    from config import settings
    from engine import engine
    from storage import storage


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
    """Return stored observation counts and collection date ranges."""
    return {
        "database": storage.path,
        "observations": storage.status(),
        "note": "Scores are research hypotheses; accumulate independent samples before use.",
    }


@mcp.tool()
def get_score_bucket_results(symbol: str, horizon_minutes: int = 60, side: str = "long") -> dict:
    """Return forward net returns grouped by score band for a side and horizon."""
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
        "warning": "Overlapping minute observations are correlated; this is preliminary research.",
    }


mcp_http_app = mcp.streamable_http_app()


async def snapshot_sampler():
    while True:
        for state in engine.states.values():
            await asyncio.to_thread(storage.insert, state.snapshot())
        await asyncio.sleep(settings.snapshot_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init()
    await engine.start()
    sampler = asyncio.create_task(snapshot_sampler())
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        sampler.cancel()
        await engine.stop()


app = FastAPI(title="Crypto Order Flow Monitor", version="0.3.0", lifespan=lifespan)


def authorize(x_api_key: str | None = Header(default=None)) -> None:
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/health")
def health():
    return {
        "ok": True,
        "version": "0.3.0",
        "symbols": settings.symbol_list,
        "feeds": {s: x.connected for s, x in engine.states.items()},
        "mcp": "/mcp/",
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
    return {
        "database": storage.path,
        "observations": storage.status(),
        "note": "至少积累数周，且每个分数组建议不少于100个样本。",
    }


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
        "warning": "连续分钟样本高度相关；正式验证将使用阈值穿越与冷却信号。",
    }


app.mount("/mcp", mcp_http_app)
