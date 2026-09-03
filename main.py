from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException

from .config import settings
from .engine import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    await engine.start()
    yield
    await engine.stop()


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

