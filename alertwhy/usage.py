"""Usage and cost ledger (an ESTIMATE from configurable assumptions, not billing data).

One JSON event per investigation is appended to data/usage_events.jsonl: federated queries, rows, model calls,
tokens (read from the AI provider's own `usage` fields) and the model that actually answered.
(This is not the Meterless integration: Meterless is the AI provider, see llm.py.)
"""
import json
import threading
import uuid
from datetime import datetime, timezone

from . import config

_lock = threading.Lock()


def _ledger():
    return config.DATA_DIR / "usage_events.jsonl"


def cost_usd(queries: int, tokens: int) -> float:
    return round(queries * config.COST_PER_QUERY_USD + tokens / 1000 * config.COST_PER_1K_TOKENS_USD, 6)


def manual_cost_usd() -> float:
    return round(config.MANUAL_MINUTES_PER_ALERT / 60 * config.ANALYST_HOURLY_USD, 2)


def record(alert_id, queries, rows, meter, duration_ms, verdict, rounds, budget_exceeded=False) -> dict:
    tokens = meter.tokens_in + meter.tokens_out
    event = {
        "id": str(uuid.uuid4()), "type": "alertwhy.investigation", "subject": alert_id,
        "time": datetime.now(timezone.utc).isoformat(),
        "data": {"queries": queries, "rows_returned": rows, "rounds": rounds, "llm_calls": meter.calls,
                 "tokens_in": meter.tokens_in, "tokens_out": meter.tokens_out,
                 "models": sorted(meter.models), "providers": sorted(meter.providers),
                 "duration_ms": round(duration_ms, 1), "cost_usd": cost_usd(queries, tokens),
                 "verdict": verdict, "budget_exceeded": budget_exceeded},
    }
    with _lock:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(_ledger(), "a") as f:
            f.write(json.dumps(event) + "\n")
    return event


def summary() -> dict:
    events = []
    p = _ledger()
    if p.exists():
        with open(p) as f:
            events = [json.loads(line) for line in f if line.strip()]
    n = len(events)
    total = sum(e["data"]["cost_usd"] for e in events)
    manual = manual_cost_usd()
    models = sorted({m for e in events for m in e["data"]["models"]})
    return {"investigations": n, "queries": sum(e["data"]["queries"] for e in events),
            "llm_calls": sum(e["data"]["llm_calls"] for e in events),
            "tokens": sum(e["data"]["tokens_in"] + e["data"]["tokens_out"] for e in events),
            "models_seen": models, "total_cost_usd": round(total, 6),
            "avg_cost_usd": round(total / n, 6) if n else 0, "manual_cost_per_alert_usd": manual,
            "estimated_savings_usd": round(n * manual - total, 2), "recent": events[-8:][::-1]}


def reset():
    p = _ledger()
    if p.exists():
        p.unlink()
