#!/usr/bin/env python3
"""All-in-one smoke test, mirrors the Participant Setup Guide's smoke_test.py (Check 3) and adds H-MEM.

    python scripts/smoke_test.py        # run while Zetaris and Nightshift are running
Reads .env (ZETARIS_REST_URL, ZETARIS_ORG_ID, ZETARIS_API_KEY, METERLESS_* or MODEL_*).
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx  # noqa: E402

from alertwhy import config  # noqa: E402
from alertwhy.hmem import AnalystMemory  # noqa: E402
from alertwhy.llm import build_router  # noqa: E402

results = []


def check(name, fn):
    try:
        detail = fn() or ""
        results.append(("PASS", name, detail))
    except Exception as e:  # noqa: BLE001
        results.append(("FAIL", name, str(e)[:200]))


def zetaris_sql():
    assert config.ZETARIS_REST_URL and config.ZETARIS_API_KEY, "ZETARIS_REST_URL / ZETARIS_API_KEY missing in .env"
    r = httpx.post(config.ZETARIS_REST_URL.rstrip("/") + "/sql-editor/sqls/run",
                   headers={"Authorization": "Bearer " + config.ZETARIS_API_KEY, "X-Org-ID": config.ZETARIS_ORG_ID,
                            "X-Request-ID": str(uuid.uuid4())},
                   json={"queryId": str(uuid.uuid4()), "sql": "SELECT 1", "source": "SqlEditor", "limit": 1}, timeout=60)
    r.raise_for_status()
    data = r.json().get("data")
    assert data and str(data[0][0]) == "1", "unexpected reply: " + r.text[:80]


def ai():
    router = build_router()
    assert router.enabled, "no AI provider configured (set METERLESS_BASE_URL + METERLESS_RUNTIME_TOKEN)"
    resp = router.chat([{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=20)
    assert resp["message"].get("content") is not None
    return f"provider={resp['provider']} model={resp['model']}"


def hmem():
    s = AnalystMemory().status()
    assert s["state"] == "online", s["error"]
    return f"{s['records']} records"


check("Data: Zetaris runs SQL over its REST API", zetaris_sql)
check("Power: AI provider replies (Meterless Nightshift / Nemotron)", ai)
check("Build: Meterless H-MEM engine answers", hmem)
for status, name, detail in results:
    print(f"{status} {name}" + (f" ({detail})" if detail else ""))
sys.exit(0 if all(s == "PASS" for s, *_ in results) else 1)
