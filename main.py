from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException

try:
    from .config import settings
    from .engine import engine
    from .storage import storage
except ImportError:  # Flat GitHub upload compatibility.
    from config import settings
    from engine import engine
    from storage import storage
import asyncio


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init()
    await engine.start()
    sampler = asyncio.create_task(snapshot_sampler())
    yield
    sampler.cancel()
    await engine.stop()


async def snapshot_sampler():
    while True:
        for state in engine.states.values():
            await asyncio.to_thread(storage.insert, state.snapshot())
        await asyncio.sleep(settings.snapshot_interval_seconds)


app = FastAPI(title="Crypto Order Flow Monitor", version="0.1.0", lifespan=lifespan)


def authorize(x_api_key: str | None = Header(default=None)) -> None:
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/health")
def health():
    return {"ok": True, "symbols": settings.symbol_list,
            "feeds": {s: x.connected for s, x in engine.states.items()}}


@app.get("/symbols", dependencies=[Depends(authorize)])
def symbols():
    return {"symbols": settings.symbol_list}


@app.get("/snapshot/{symbol}", dependencies=[Depends(authorize)])
def snapshot(symbol: str):
    normalized = symbol.upper().replace("/", "")
    if not normalized.endswith("USDT"): normalized += "USDT"
    state = engine.states.get(normalized)
    if not state:
        raise HTTPException(status_code=404, detail=f"Supported: {', '.join(settings.symbol_list)}")
    return state.snapshot()


@app.get("/snapshot", dependencies=[Depends(authorize)])
def all_snapshots():
    return {s: state.snapshot() for s, state in engine.states.items()}


@app.get("/research/status", dependencies=[Depends(authorize)])
def research_status():
    return {"database": storage.path, "observations": storage.status(),
            "note": "至少积累数周，且每个分数组建议不少于100个样本。"}


@app.get("/research/score-buckets/{symbol}", dependencies=[Depends(authorize)])
def score_buckets(symbol: str, horizon: int = 60, side: str = "long"):
    normalized = symbol.upper().replace("/", "")
    if not normalized.endswith("USDT"): normalized += "USDT"
    if normalized not in engine.states:
        raise HTTPException(status_code=404, detail="Unsupported symbol")
    try:
        rows = storage.buckets(normalized, horizon, side.lower())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"symbol": normalized, "side": side.lower(), "horizon_minutes": horizon,
            "round_trip_cost_pct": settings.round_trip_cost_pct, "buckets": rows,
            "warning": "连续分钟样本高度相关；正式验证将使用阈值穿越与冷却信号。"}
