"""DEVELOPMENT ONLY: serves MockNightshift over real HTTP so the UI's Connect Meterless flow can be exercised in a browser
without Nightshift. It is NOT Meterless and must never be used to demonstrate or claim the integration.

  python tests/mock_nightshift_server.py 18088      # token: runtime-token
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request, Response  # noqa: E402
from mocks import MockNightshift  # noqa: E402

ns = MockNightshift()
ns.plan = {"checks": ["pass_through", "geography", "sanctions"], "rationale": "layering pattern: check flows, jurisdictions and screening"}
ns.explore_sql = ["SELECT counterparty_name AS counterparty, COUNT(*) AS cnt FROM core_transactions "
                  "WHERE counterparty_country IN ('PA','KY','VG') GROUP BY counterparty_name ORDER BY cnt DESC LIMIT 5"]
app = FastAPI()


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def any_path(path: str, request: Request):
    req = httpx.Request(request.method, str(request.url), headers=dict(request.headers), content=await request.body())
    r = ns.handler(req)
    return Response(r.content, status_code=r.status_code, media_type="application/json")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 18088, log_level="warning")
