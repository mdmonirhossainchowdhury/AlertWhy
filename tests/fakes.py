"""Fake sponsor servers, as httpx MockTransports. They ENFORCE the documented contracts, so the adapters are tested
against the contract (they do not prove the real servers behave identically; scripts/smoke_test.py does that live).

FakeZetaris   POST {base}/sql-editor/sqls/run  with  Authorization: Bearer <key>, X-Org-ID: <digits>,
              X-Request-ID: <uuid>, body {queryId: uuid, sql, source: "SqlEditor", limit}.
              Executes the (already rewritten, Spark-flavoured) SQL on a DuckDB that has the three Zetaris
              datasources as schemas core_pg / kyc_pg / screen_pg, exactly as zetaris_setup.py registers them.
FakeNightshift  OpenAI-compatible Meterless Runtime: GET /v1/models, POST /v1/chat/completions, bearer token,
              `meterless-app` header. Scriptable planner / critic / explorer / narrator behaviour.
"""
import json
import re
import uuid

import duckdb
import httpx


class FakeZetaris:
    def __init__(self, data_dir, api_key="zk-test", org_id="1", shape="lists", stringify=True):
        d = str(data_dir)
        self.api_key, self.org_id, self.shape, self.stringify = api_key, org_id, shape, stringify
        self.requests, self.sqls = [], []
        con = self.con = duckdb.connect()
        con.execute(f"ATTACH '{d}/core_banking.duckdb' AS src (READ_ONLY)")
        for s in ("core_pg", "kyc_pg", "screen_pg"):
            con.execute(f"CREATE SCHEMA {s}")
        for t in ("accounts", "transactions", "alerts"):
            con.execute(f"CREATE TABLE core_pg.{t} AS SELECT * FROM src.{t}")
        con.execute(f"CREATE TABLE kyc_pg.customers AS SELECT * FROM read_parquet('{d}/kyc_customers.parquet')")
        con.execute(f"CREATE TABLE screen_pg.sanctions AS SELECT * FROM read_csv_auto('{d}/sanctions_list.csv', all_varchar=true)")
        con.execute(f"CREATE TABLE screen_pg.adverse_media AS SELECT entity, country, CAST(date AS DATE) AS date, "
                    f"headline, category, severity, source FROM read_json_auto('{d}/adverse_media.jsonl', format='newline_delimited')")
        con.execute(f"CREATE TABLE screen_pg.jurisdictions AS SELECT * FROM read_csv_auto('{d}/jurisdictions.csv')")
        con.execute("CREATE MACRO percentile_approx(x, q) AS quantile_cont(x, q)")  # Spark function

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if not req.url.path.endswith("/sql-editor/sqls/run") or req.method != "POST":
            return httpx.Response(404, text="not found")
        if req.headers.get("authorization") != f"Bearer {self.api_key}":
            return httpx.Response(401, text="unauthorized")
        org, rid = req.headers.get("x-org-id", ""), req.headers.get("x-request-id", "")
        try:
            uuid.UUID(rid)
        except ValueError:
            return httpx.Response(400, text="X-Request-ID must be a UUID")
        if not re.fullmatch(r"[0-9]+", org):
            return httpx.Response(400, text="bad X-Org-ID")
        body = json.loads(req.content)
        assert set(body) >= {"queryId", "sql", "source", "limit"}, body.keys()
        uuid.UUID(body["queryId"])
        assert body["source"] == "SqlEditor"
        self.sqls.append(body["sql"])
        try:
            cur = self.con.execute(body["sql"])
        except Exception as e:  # noqa: BLE001
            return httpx.Response(400, text=f"SQL error: {e}")
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
        conv = (lambda v: None if v is None else str(v)) if self.stringify else (lambda v: v)
        data = [[conv(v) for v in r] for r in rows]
        if self.shape == "objects":
            return httpx.Response(200, json={"data": [dict(zip(cols, r)) for r in data]})
        if self.shape == "columns":
            return httpx.Response(200, json={"data": data, "columns": [{"name": c} for c in cols]})
        return httpx.Response(200, json={"data": data})  # worst case: no column names at all


class FakeNightshift:
    def __init__(self, token="tok-test", plan=None, critic=None, explore_sql=None, narrative=None, fail=None):
        self.token = token
        self.plan = plan or ["sanctions", "history", "geography"]
        self.critic = list(critic or [])          # list of additional_checks lists, consumed in order
        self.explore_sql = explore_sql or ("SELECT counterparty_name, COUNT(*) AS n FROM core_transactions "
                                           "GROUP BY counterparty_name ORDER BY n DESC")
        self.narrative = narrative                # override text for the narrator
        self.fail = fail                          # None | "offline" | "500"
        self.calls, self.headers_seen = [], []

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _chat(self, text=None, tool_calls=None):
        msg = {"role": "assistant", "content": text}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return httpx.Response(200, json={"id": "x", "model": "fake/nemotron-test", "choices": [{"message": msg}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 50}})

    def _handle(self, req: httpx.Request) -> httpx.Response:
        if self.fail == "offline":
            raise httpx.ConnectError("refused")
        if self.fail == "500":
            return httpx.Response(500, text="boom")
        if req.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, text="unauthorized")
        self.headers_seen.append(dict(req.headers))
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "meterless/auto"}, {"id": "meterless/fast.balanced"}]})
        body = json.loads(req.content)
        self.calls.append(body)
        msgs, last = body["messages"], body["messages"][-1]
        content = last.get("content") or ""
        if body.get("tools"):
            if last["role"] == "tool":
                return self._chat("Nothing else unusual.")
            return self._chat(None, [{"id": "call_1", "type": "function",
                                      "function": {"name": "run_sql", "arguments": json.dumps({"sql": self.explore_sql})}}])
        if "Choose 3-5 checks" in content:
            return self._chat(json.dumps({"checks": self.plan, "rationale": "fake planner"}))
        if "skeptical reviewer" in content:
            add = self.critic.pop(0) if self.critic else []
            return self._chat(json.dumps({"approve": not add, "additional_checks": add, "reason": "fake critic"}))
        if "case rationale" in content:
            if self.narrative is not None:
                return self._chat(self.narrative)
            ids = re.findall(r"^\[(E\d+)\]", content, re.M)
            return self._chat("The decision rests on the evidence " + " and ".join(f"[{i}]" for i in ids[:3]) + ".")
        return self._chat("ok")
