"""Test doubles for the two sponsor services, built from the contracts in the hackathon Participant Setup Guide.

MockZetaris   POST {base}/sql-editor/sqls/run with `Authorization: Bearer <key>`, `X-Org-ID` (numeric),
              `X-Request-ID` (fresh UUID) and JSON {queryId, sql, source:"SqlEditor", limit}. Replies {"data": [[...]]}
              with every value rendered as a STRING (worst case: the guide's smoke test does str(data[0][0]) == "1").
              It runs the SQL on DuckDB but behaves like a Spark engine in the ways that matter here: tables must be
              datasource-qualified (core_pg.transactions ...), and DuckDB-only functions are rejected with HTTP 400.

MockNightshift  OpenAI-compatible Meterless Runtime: GET /v1/models, POST /v1/chat/completions (with tool calls),
              bearer token, `meterless-app` header. Its replies are scripted per prompt type (plan / critic /
              explore / narrate) so tests can drive the agent loop deterministically.

These prove the adapters follow the documented contract. They do NOT prove the real services behave identically:
run scripts/smoke_test.py against the real ones.
"""
import json
import re
import uuid
from datetime import date, datetime

import duckdb
import httpx

from alertwhy import zsetup


def _str(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


class MockZetaris:
    def __init__(self, data_dir, api_key="test-key", org_id="1", with_columns=False):
        self.api_key, self.org_id, self.with_columns = api_key, org_id, with_columns
        self.requests = []     # (headers, body) of every accepted call
        self.statements = []   # SQL strings received
        self.con = duckdb.connect()
        self.con.execute(f"ATTACH '{data_dir}/core_banking.duckdb' AS core (READ_ONLY)")
        queries = zsetup.source_queries(data_dir)
        for db, spec in zsetup.DATABASES.items():
            self.con.execute(f"CREATE SCHEMA {spec['ds']}")
            for table in spec["tables"]:
                self.con.execute(f"CREATE TABLE {spec['ds']}.{table} AS {queries[table]}")

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler), timeout=5)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or not request.url.path.endswith("/sql-editor/sqls/run"):
            return httpx.Response(404, text="not found")
        if request.headers.get("authorization") != f"Bearer {self.api_key}":
            return httpx.Response(401, json={"message": "unauthorized"})
        org = request.headers.get("x-org-id", "")
        try:
            uuid.UUID(request.headers.get("x-request-id", ""))
        except ValueError:
            return httpx.Response(400, json={"message": "X-Request-ID must be a UUID"})
        if org != self.org_id:
            return httpx.Response(400, json={"message": "bad X-Org-ID"})
        body = json.loads(request.content)
        assert set(body) >= {"queryId", "sql", "source", "limit"}, body
        uuid.UUID(body["queryId"])
        assert body["source"] == "SqlEditor"
        sql = body["sql"]
        self.statements.append(sql)
        self.requests.append((dict(request.headers), body))
        if ";" in re.sub(r"'(?:[^'\\]|\\.)*'", "''", sql):
            return httpx.Response(400, json={"message": "one statement per call"})
        if re.search(r"(?i)quantile_cont|read_parquet|read_csv", sql):
            return httpx.Response(400, json={"message": "AnalysisException: undefined function (not Spark SQL)"})
        spark = re.sub(r"(?i)percentile_approx\(", "quantile_cont(", sql)
        spark = spark.replace("\\'", "''").replace("\\\\", "\\")
        try:
            cur = self.con.execute(spark)
        except duckdb.Error as e:
            return httpx.Response(400, json={"message": f"AnalysisException: {e}"})
        rows = cur.fetchall()[: int(body["limit"])]
        out = {"data": [[_str(v) for v in r] for r in rows]}
        if self.with_columns:
            out["columns"] = [{"name": c[0]} for c in cur.description]
        return httpx.Response(200, json=out)


class MockNightshift:
    """Scripted Meterless Runtime. Configure behaviour through attributes before use."""

    def __init__(self, token="runtime-token", model_name="nemotron-mock-8b"):
        self.token, self.model_name = token, model_name
        self.offline = False
        self.plan = {"checks": ["structuring", "history", "geography"], "rationale": "mock plan"}
        self.plan_raw = None            # if set, returned verbatim instead of JSON (to test invalid output)
        self.critic = {"approve": True, "additional_checks": [], "reason": "mock critic approves"}
        self.explore_sql = []           # SQL strings the model "decides" to run, one per tool call
        self.cite = "valid"             # valid | invented | none
        self.calls = []                 # (kind, headers) for every chat request
        self.models = ["meterless/auto", "meterless/fast.think"]

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler), timeout=5)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("refused", request=request)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "bad token"})
        path = request.url.path
        if request.method == "GET" and path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        if request.method == "POST" and path.endswith("/v1/chat/completions"):
            body = json.loads(request.content)
            return self._chat(body, dict(request.headers))
        return httpx.Response(404, text="not found")

    def _reply(self, content=None, tool_calls=None, tin=120, tout=40):
        msg = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return httpx.Response(200, json={"model": self.model_name, "choices": [{"message": msg}],
                                         "usage": {"prompt_tokens": tin, "completion_tokens": tout}})

    def _chat(self, body, headers):
        msgs = body["messages"]
        user = next(m["content"] for m in reversed(msgs) if m["role"] == "user")
        if body.get("tools"):
            self.calls.append(("explore", headers))
            done = sum(1 for m in msgs if m["role"] == "tool")
            if done < len(self.explore_sql):
                call = {"id": f"call{done}", "type": "function",
                        "function": {"name": "run_sql", "arguments": json.dumps({"sql": self.explore_sql[done]})}}
                return self._reply(None, [call])
            return self._reply("Nothing further unusual.")
        if "Choose 3-5 checks" in user:
            self.calls.append(("plan", headers))
            return self._reply(self.plan_raw if self.plan_raw is not None else json.dumps(self.plan))
        if "skeptical reviewer" in user:
            self.calls.append(("critic", headers))
            return self._reply(json.dumps(self.critic))
        if "case rationale" in user:
            self.calls.append(("narrate", headers))
            ids = re.findall(r"\[(E\d+)\]", user)
            if self.cite == "none":
                return self._reply("The alert looks suspicious.")
            if self.cite == "invented":
                return self._reply("Flagged because of structuring [E99].")
            return self._reply("Recommendation based on the evidence " + " ".join(f"[{i}]" for i in ids[:2]) + ".")
        self.calls.append(("other", headers))
        return self._reply("Connection works.")
