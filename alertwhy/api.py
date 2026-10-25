"""FastAPI app. Run:  uvicorn alertwhy.api:app --port 8000"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, datagen, usage
from .agent import Investigator
from .cli import evaluate
from .federation import FederationError
from .hmem import MemoryUnavailable, seed_demo_history

STATIC = Path(__file__).resolve().parent.parent / "static"
state = {}


@asynccontextmanager
async def lifespan(app):
    if not (config.DATA_DIR / "core_banking.duckdb").exists():
        datagen.build()
    state["inv"] = Investigator()
    yield
    if state["inv"].memory:
        state["inv"].memory.client.close()


app = FastAPI(title="AlertWhy", version="2.0.0", lifespan=lifespan)


class Feedback(BaseModel):
    decision: str
    note: str = ""


def _guard(fn):
    try:
        return fn()
    except KeyError as e:
        raise HTTPException(404, f"Not found: {e}")
    except FederationError as e:
        raise HTTPException(502, f"Data layer error ({e.kind}): {e}")
    except MemoryUnavailable as e:
        raise HTTPException(503, f"Memory engine unavailable: {e}")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/health")
def health():
    inv = state["inv"]
    return {"status": "ok", "federation_backend": inv.fed.backend, "federation_kind": inv.fed.kind,
            "ai_enabled": inv.router.enabled, "providers": [p.name for p in inv.router.providers],
            "memory_enabled": bool(inv.memory)}


@app.get("/api/status")
def status():
    """Live check of all three sponsor layers (Zetaris, Meterless provider, H-MEM engine)."""
    return _guard(state["inv"].sponsor_status)


@app.get("/api/alerts")
def alerts():
    rows = _guard(state["inv"].list_alerts)
    return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in a.items()} for a in rows]


@app.post("/api/alerts/{alert_id}/investigate")
def investigate(alert_id: str, memory: bool = True):
    return _guard(lambda: state["inv"].investigate(alert_id, use_memory=memory))


@app.post("/api/alerts/{alert_id}/feedback")
def feedback(alert_id: str, body: Feedback):
    return _guard(lambda: state["inv"].analyst_feedback(alert_id, body.decision.upper(), body.note))


@app.get("/api/memory")
def memory():
    inv = state["inv"]
    if not inv.memory:
        return {"enabled": False, "records": []}
    return _guard(lambda: {"enabled": True, "records": inv.memory.all()})


@app.post("/api/memory/seed")
def memory_seed():
    inv = state["inv"]
    if not inv.memory:
        raise HTTPException(400, "memory is disabled")
    return _guard(lambda: {"created": seed_demo_history(inv.memory)})


@app.post("/api/memory/reset")
def memory_reset():
    inv = state["inv"]
    if inv.memory:
        inv.memory.reset()
    return {"ok": True}


@app.get("/api/usage")
def usage_summary():
    return usage.summary()


@app.post("/api/usage/reset")
def usage_reset():
    usage.reset()
    return {"ok": True}


@app.post("/api/evaluate")
def run_evaluation():
    usage.reset()
    return _guard(lambda: evaluate(state["inv"]))


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
