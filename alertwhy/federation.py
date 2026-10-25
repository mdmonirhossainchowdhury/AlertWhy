"""Federated query layer: the Zetaris integration point.

The agent only ever calls `Federation.query(sql, params, log)`. All SQL is written against LOGICAL table names
(core_transactions, kyc, sanctions ...). Two backends:

* ZetarisFederation - the real thing. POSTs each statement to the Zetaris REST API
      POST {ZETARIS_REST_URL}/sql-editor/sqls/run
      Authorization: Bearer <api key> | X-Org-ID: <org id> | X-Request-ID: <fresh uuid>
      body {"queryId": <uuid>, "sql": "...", "source": "SqlEditor", "limit": N}
  (contract from the hackathon Participant Setup Guide, section 1.9 and smoke_test.py).
  Logical names are rewritten to Zetaris datasource tables (core_pg.transactions ...), parameters are bound as
  escaped literals (the REST call takes plain SQL), and Spark-SQL differences go through `dialect`.
* LocalFederation - OFFLINE FALLBACK ONLY: DuckDB over the same five files, in place. It exists so the project
  runs and tests without Docker. It is NOT Zetaris and the UI says so.
"""
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import duckdb
import httpx

from . import config

LOGICAL_TABLES = ["core_accounts", "core_transactions", "core_alerts", "kyc", "sanctions", "media", "jurisdictions"]

DEFAULT_TABLE_MAP = {
    "core_accounts": "core_pg.accounts",
    "core_transactions": "core_pg.transactions",
    "core_alerts": "core_pg.alerts",
    "kyc": "kyc_pg.customers",
    "sanctions": "screen_pg.sanctions",
    "media": "screen_pg.adverse_media",
    "jurisdictions": "screen_pg.jurisdictions",
}

TABLE_COLUMNS = {
    "core_accounts": ["account_id", "customer_id", "opened", "status"],
    "core_transactions": ["txn_id", "account_id", "customer_id", "ts", "direction", "amount_usd", "channel",
                          "counterparty_name", "counterparty_country"],
    "core_alerts": ["alert_id", "customer_id", "rule_id", "rule_desc", "triggered_at", "trigger_txn_ids"],
    "kyc": ["customer_id", "name", "dob", "country", "kind", "occupation", "onboarded", "expected_monthly_usd",
            "risk_rating"],
    "sanctions": ["entity_id", "name", "aliases", "dob", "country", "list_name"],
    "media": ["entity", "country", "date", "headline", "category", "severity", "source"],
    "jurisdictions": ["country", "risk_tier"],
}

# Column-name -> python type, used when a backend returns everything as strings (the REST smoke test shows "1").
_FLOATS = {"amount_usd", "total_usd", "expected_monthly_usd", "p90_usd"}
_INTS = {"cnt"}
_DATETIMES = {"ts", "triggered_at"}
_DATES = {"onboarded", "opened", "day"}


class FederationError(RuntimeError):
    def __init__(self, message, kind="error"):
        super().__init__(message)
        self.kind = kind


@dataclass
class QueryRecord:
    sql: str
    params: list
    rows: int
    ms: float


@dataclass
class QueryLog:
    records: list = field(default_factory=list)

    @property
    def count(self):
        return len(self.records)

    @property
    def rows(self):
        return sum(r.rows for r in self.records)


# ------------------------------------------------------------------ dialects
class DuckDialect:
    name = "duckdb"

    @staticmethod
    def pct(col: str, q: float) -> str:
        return f"quantile_cont({col}, {q})"


class SparkDialect:
    """Zetaris Lightning SQL is Spark SQL."""
    name = "spark"

    @staticmethod
    def pct(col: str, q: float) -> str:
        return f"percentile_approx({col}, {q})"


# ------------------------------------------------------------------ SQL text helpers
_QUOTED = re.compile(r"('(?:[^'\\]|\\.|'')*')")


def _segments(sql: str):
    """Yield (is_quoted, text) so rewrites never touch string literals."""
    for i, part in enumerate(_QUOTED.split(sql)):
        if part:
            yield (i % 2 == 1, part)


def literal(v) -> str:
    """Render a Python value as a Spark-SQL literal. Strings are escaped (backslash and quote)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            raise FederationError("Cannot bind a non-finite number")
        return repr(v)
    if isinstance(v, datetime):
        return f"TIMESTAMP '{v.strftime('%Y-%m-%d %H:%M:%S')}'"
    if isinstance(v, date):
        return f"DATE '{v.isoformat()}'"
    s = str(v)
    if any(ord(c) < 32 for c in s):
        raise FederationError("Control characters are not allowed in bound values")
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def bind(sql: str, params) -> str:
    """Replace each unquoted ? with a literal. Parameter count must match."""
    params = list(params or [])
    out, i = [], 0
    for quoted, part in _segments(sql):
        if quoted:
            out.append(part)
            continue
        pieces = part.split("?")
        out.append(pieces[0])
        for piece in pieces[1:]:
            if i >= len(params):
                raise FederationError("More ? placeholders than parameters")
            out.append(literal(params[i]))
            i += 1
            out.append(piece)
    if i != len(params):
        raise FederationError("More parameters than ? placeholders")
    return "".join(out)


def rewrite_tables(sql: str, table_map: dict) -> str:
    """Rewrite logical table names after FROM / JOIN (outside string literals)."""
    names = "|".join(sorted(map(re.escape, table_map), key=len, reverse=True))
    pat = re.compile(rf"(?i)\b(from|join)(\s+)({names})\b")
    out = []
    for quoted, part in _segments(sql):
        out.append(part if quoted else pat.sub(lambda m: f"{m.group(1)}{m.group(2)}{table_map[m.group(3).lower()]}", part))
    return "".join(out)


def _top_level_select_list(sql: str):
    """Return (select_list_text, from_table_or_None) for the first top-level SELECT, or None.
    A SELECT with no FROM (e.g. `SELECT 1 AS one`) has its list run to the end of the statement."""
    flat = "".join(("''" if q else p) for q, p in _segments(sql))
    m = re.search(r"(?is)\bselect\b", flat)
    if not m:
        return None
    start, depth, i, end = m.end(), 0, m.end(), None
    while i < len(flat):
        c = flat[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and re.match(r"(?i)\bfrom\b", flat[i:i + 5]) and (i == 0 or not flat[i - 1].isalnum()):
            end = i
            break
        i += 1
    if end is None:
        tail = re.split(r"(?i)\b(?:limit|order\s+by|where|group\s+by)\b", flat[start:], maxsplit=1)[0]
        return tail, None
    rest = flat[end + 4:]
    t = re.match(r"\s+([A-Za-z_][\w.]*)", rest)
    return flat[start:end], (t.group(1).lower() if t else None)


def derive_columns(sql: str, table_map=None):
    """Column names of a SELECT from its own text (aliases), for backends that return rows without names."""
    parsed = _top_level_select_list(sql)
    if not parsed:
        return None
    text, table = parsed
    parts, depth, cur = [], 0, ""
    for c in text:
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
    parts.append(cur)
    cols = []
    inverse = {v.lower(): k for k, v in (table_map or {}).items()}
    for p in parts:
        p = p.strip()
        if p == "*":
            logical = table if table in TABLE_COLUMNS else inverse.get(table or "")
            if logical not in TABLE_COLUMNS:
                return None
            cols.extend(TABLE_COLUMNS[logical])
            continue
        m = re.search(r"(?is)\s+as\s+([A-Za-z_]\w*)\s*$", p)
        if m:
            cols.append(m.group(1))
        elif re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)?", p):
            cols.append(p.split(".")[-1])
        else:
            return None
    return cols


_TS = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}:\d{2}))?")


def coerce(col: str, v):
    """REST rows may come back as strings (the setup guide's smoke test compares str(data[0][0]) == "1").
    Convert by column NAME. Timezone suffixes are dropped so values compare with the naive as-of date."""
    if v is None:
        return v
    try:
        if col in _FLOATS:
            return float(v)
        if col in _INTS:
            return int(float(v))
        if col in _DATETIMES or col in _DATES:
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 1e10:  # epoch milliseconds
                dt = datetime.fromtimestamp(v / 1000, timezone.utc).replace(tzinfo=None)
                return dt if col in _DATETIMES else dt.date()
            if isinstance(v, str):
                m = _TS.match(v.strip())
                if m:
                    dt = datetime.fromisoformat(f"{m.group(1)}T{m.group(2) or '00:00:00'}")
                    return dt if col in _DATETIMES else dt.date()
    except (ValueError, OverflowError, OSError):
        return v
    return v


# ------------------------------------------------------------------ backends
class LocalFederation:
    backend = "local-duckdb (offline fallback, NOT Zetaris)"
    kind = "local"
    dialect = DuckDialect

    def __init__(self, data_dir=None):
        d = data_dir or config.DATA_DIR
        self._lock = threading.Lock()
        self.con = duckdb.connect()
        self.con.execute(f"ATTACH '{d}/core_banking.duckdb' AS core (READ_ONLY)")
        self.con.execute("CREATE VIEW core_accounts AS SELECT * FROM core.accounts")
        self.con.execute("CREATE VIEW core_transactions AS SELECT * FROM core.transactions")
        self.con.execute("CREATE VIEW core_alerts AS SELECT * FROM core.alerts")
        self.con.execute(f"CREATE VIEW kyc AS SELECT * FROM read_parquet('{d}/kyc_customers.parquet')")
        self.con.execute(f"CREATE VIEW sanctions AS SELECT * FROM "
                         f"read_csv_auto('{d}/sanctions_list.csv', all_varchar=true)")
        self.con.execute(f"CREATE VIEW jurisdictions AS SELECT * FROM read_csv_auto('{d}/jurisdictions.csv')")
        self.con.execute(f"CREATE VIEW media AS SELECT * FROM read_json_auto('{d}/adverse_media.jsonl', "
                         f"format='newline_delimited')")

    def query(self, sql, params=None, log: QueryLog = None):
        t0 = time.perf_counter()
        with self._lock:
            try:
                cur = self.con.execute(sql, params or [])
            except duckdb.Error as e:
                raise FederationError(f"SQL error: {e}", "sql")
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        if log is not None:
            log.records.append(QueryRecord(sql, list(params or []), len(rows), (time.perf_counter() - t0) * 1000))
        return rows

    def check(self):
        try:
            tables = {t: self.query(f"SELECT COUNT(*) AS cnt FROM {t}")[0]["cnt"] for t in LOGICAL_TABLES}
            return {"reachable": True, "select1": True, "tables": tables, "error": None}
        except Exception as e:  # pragma: no cover - defensive
            return {"reachable": False, "select1": False, "tables": {}, "error": str(e)}


class ZetarisFederation:
    backend = "Zetaris (REST, live)"
    kind = "zetaris"
    dialect = SparkDialect

    def __init__(self, rest_url=None, api_key=None, org_id=None, table_map=None, client=None):
        # None means "use config"; an explicit empty string must still fail validation.
        url = config.ZETARIS_REST_URL if rest_url is None else rest_url
        key = config.ZETARIS_API_KEY if api_key is None else api_key
        oid = config.ZETARIS_ORG_ID if org_id is None else org_id
        self.rest_url = (url or "").rstrip("/")
        self.api_key = key or ""
        self.org_id = str(oid or "")
        if not self.rest_url:
            raise FederationError("ZETARIS_REST_URL is not set (e.g. http://localhost:8888/api/v1.0)", "config")
        if not self.api_key:
            raise FederationError("ZETARIS_API_KEY is not set: create it yourself in the Zetaris web UI", "config")
        if not re.fullmatch(r"[0-9]+", self.org_id):
            raise FederationError("ZETARIS_ORG_ID must be numeric (1 on a local install)", "config")
        self.table_map = {**DEFAULT_TABLE_MAP, **config.ZETARIS_TABLE_MAP, **(table_map or {})}
        self.client = client or httpx.Client(timeout=config.ZETARIS_TIMEOUT)
        self.last_shape = None  # keys of the last response, for diagnostics (never values)

    # ---- low-level REST
    def run_raw(self, sql: str):
        headers = {"Authorization": f"Bearer {self.api_key}", "X-Org-ID": self.org_id,
                   "X-Request-ID": str(uuid.uuid4()), "Content-Type": "application/json"}
        body = {"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": config.ZETARIS_QUERY_LIMIT}
        try:
            r = self.client.post(f"{self.rest_url}/sql-editor/sqls/run", json=body, headers=headers)
        except httpx.HTTPError as e:
            raise FederationError(f"Cannot reach Zetaris at {self.rest_url}: {type(e).__name__}. "
                                  f"Is `docker compose up -d` running?", "offline")
        if r.status_code == 401:
            raise FederationError("Zetaris returned 401: the API key is missing, wrong or expired. "
                                  "Create a new key in the Zetaris web UI and update .env", "unauthorized")
        if r.status_code == 400:
            raise FederationError(f"Zetaris returned 400 (check ZETARIS_ORG_ID is 1 and X-Request-ID is a UUID, "
                                  f"or the SQL is invalid): {r.text[:300]}", "bad_request")
        if r.status_code >= 400:
            raise FederationError(f"Zetaris returned HTTP {r.status_code}: {r.text[:300]}", "http")
        try:
            return r.json()
        except ValueError:
            raise FederationError("Zetaris returned a non-JSON response", "bad_response")

    @staticmethod
    def _names(x):
        out = []
        for c in x:
            if isinstance(c, str):
                out.append(c)
            elif isinstance(c, dict):
                n = c.get("name") or c.get("columnName") or c.get("field") or c.get("label")
                if not n:
                    return None
                out.append(str(n))
            else:
                return None
        return out

    def parse(self, payload, sql: str):
        """Turn a REST payload into list[dict]. Handles rows as lists or dicts, and several column-name spellings;
        falls back to the aliases in the SQL itself. Everything funnels through this one function."""
        if isinstance(payload, dict):
            self.last_shape = sorted(payload.keys())
        data = payload.get("data") if isinstance(payload, dict) else payload
        if data is None:
            data = []
        if isinstance(data, dict):  # {"columns": [...], "rows": [...]} nested
            payload, data = data, data.get("rows") or data.get("data") or []
        if data and isinstance(data[0], dict):
            return [{k: coerce(k, v) for k, v in row.items()} for row in data]
        cols = None
        if isinstance(payload, dict):
            for key in ("columns", "columnNames", "column_names", "headers", "header", "schema", "fields", "meta"):
                v = payload.get(key)
                if isinstance(v, dict):
                    v = v.get("fields") or v.get("columns")
                if isinstance(v, list) and v:
                    cols = self._names(v)
                    if cols:
                        break
        if not cols:
            cols = derive_columns(sql, self.table_map)
        if not data:
            return []
        if not cols or len(cols) != len(data[0]):
            raise FederationError(
                f"Cannot name the result columns (response keys: {self.last_shape}). Run "
                f"`python scripts/zetaris_probe.py` and adjust ZetarisFederation.parse() to the real response shape.",
                "bad_response")
        return [{c: coerce(c, v) for c, v in zip(cols, row)} for row in data]

    def translate(self, sql, params=None) -> str:
        return rewrite_tables(bind(sql, params), self.table_map)

    def query(self, sql, params=None, log: QueryLog = None):
        t0 = time.perf_counter()
        native = self.translate(sql, params)
        rows = self.parse(self.run_raw(native), sql)
        if log is not None:
            log.records.append(QueryRecord(native, [], len(rows), (time.perf_counter() - t0) * 1000))
        return rows

    def check(self):
        out = {"reachable": False, "select1": False, "tables": {}, "error": None}
        try:
            r = self.run_raw("SELECT 1 AS one")
            out["reachable"] = True
            data = r.get("data") if isinstance(r, dict) else None
            out["select1"] = bool(data) and str(data[0][0] if isinstance(data[0], list) else list(data[0].values())[0]) == "1"
            for t in LOGICAL_TABLES:
                try:
                    out["tables"][t] = self.query(f"SELECT COUNT(*) AS cnt FROM {t}")[0]["cnt"]
                except FederationError as e:
                    out["tables"][t] = f"ERROR: {str(e)[:120]}"
        except FederationError as e:
            out["error"] = str(e)
        return out


def get_federation(data_dir=None):
    backend = config.FEDERATION_BACKEND
    if backend == "auto":
        backend = "zetaris" if (config.ZETARIS_REST_URL and config.ZETARIS_API_KEY) else "local"
    if backend == "zetaris":
        return ZetarisFederation()
    return LocalFederation(data_dir)
