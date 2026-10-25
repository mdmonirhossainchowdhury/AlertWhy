"""Zetaris adapter: SQL translation, REST contract, response parsing, errors, and full-benchmark parity."""
import csv
import re
import uuid
from datetime import date, datetime

import httpx
import pytest

from alertwhy import config
from alertwhy.agent import Investigator
from alertwhy.federation import (DEFAULT_TABLE_MAP, FederationError, ZetarisFederation, bind, coerce,
                                 derive_columns, literal, rewrite_tables)
from alertwhy.llm import Router
from fakes import FakeZetaris


# ---------------------------------------------------------------- SQL translation
def test_literal_escaping_is_spark_style():
    assert literal("O'Brien") == "'O\\'Brien'"
    assert literal("a\\b") == "'a\\\\b'"
    assert literal(None) == "NULL" and literal(True) == "TRUE" and literal(5) == "5"
    assert literal(datetime(2026, 10, 15, 23, 59, 59)) == "TIMESTAMP '2026-10-15 23:59:59'"
    assert literal(date(2026, 10, 15)) == "DATE '2026-10-15'"


def test_literal_rejects_control_chars_and_nan():
    with pytest.raises(FederationError):
        literal("a\nb")
    with pytest.raises(FederationError):
        literal(float("nan"))


def test_bind_skips_question_marks_inside_string_literals():
    assert bind("SELECT * FROM kyc WHERE name = 'what?' AND customer_id = ?", ["C1"]) == \
        "SELECT * FROM kyc WHERE name = 'what?' AND customer_id = 'C1'"


def test_bind_checks_parameter_count():
    with pytest.raises(FederationError):
        bind("SELECT ? , ?", [1])
    with pytest.raises(FederationError):
        bind("SELECT ?", [1, 2])


def test_sql_injection_via_parameter_stays_a_literal():
    out = bind("SELECT * FROM kyc WHERE name = ?", ["x' OR '1'='1"])
    assert out == "SELECT * FROM kyc WHERE name = 'x\\' OR \\'1\\'=\\'1'"


def test_rewrite_tables_only_after_from_join_and_outside_strings():
    sql = "SELECT 'from kyc' AS s, kyc.name FROM kyc JOIN core_alerts a ON 1=1 WHERE note = 'join sanctions'"
    out = rewrite_tables(sql, DEFAULT_TABLE_MAP)
    assert "FROM kyc_pg.customers" in out and "JOIN core_pg.alerts a" in out
    assert "'from kyc'" in out and "'join sanctions'" in out
    assert "kyc.name" in out


def test_derive_columns_from_aliases_and_star():
    assert derive_columns("SELECT COUNT(*) AS cnt FROM kyc") == ["cnt"]
    assert derive_columns("SELECT a.alert_id, k.name AS customer_name FROM core_alerts a JOIN kyc k USING (x)") == \
        ["alert_id", "customer_name"]
    assert derive_columns("SELECT * FROM kyc")[:2] == ["customer_id", "name"]
    assert derive_columns("SELECT COUNT(*) FROM kyc") is None


def test_coerce():
    assert coerce("amount_usd", "12.5") == 12.5
    assert coerce("cnt", "7") == 7
    assert coerce("ts", "2026-10-14 10:51:00") == datetime(2026, 10, 14, 10, 51)
    assert coerce("onboarded", "2024-01-02") == date(2024, 1, 2)
    assert coerce("ts", 1760439060000).year == 2025  # epoch milliseconds
    assert coerce("name", "7") == "7"


# ---------------------------------------------------------------- REST contract
def make(fz, **kw):
    return ZetarisFederation(rest_url="http://localhost:8888/api/v1.0", api_key=fz.api_key, org_id="1",
                             client=fz.client(), **kw)


def test_rest_contract_headers_and_body(data_dir):
    fz = FakeZetaris(data_dir)
    z = make(fz)
    assert z.query("SELECT COUNT(*) AS cnt FROM kyc")[0]["cnt"] == 289
    r = fz.requests[-1]
    assert str(r.url) == "http://localhost:8888/api/v1.0/sql-editor/sqls/run"
    assert r.headers["authorization"] == "Bearer zk-test" and r.headers["x-org-id"] == "1"
    uuid.UUID(r.headers["x-request-id"])
    ids = {q.headers["x-request-id"] for q in fz.requests}
    z.query("SELECT COUNT(*) AS cnt FROM kyc")
    assert fz.requests[-1].headers["x-request-id"] not in ids  # fresh UUID per call
    assert fz.sqls[-1] == "SELECT COUNT(*) AS cnt FROM kyc_pg.customers"


@pytest.mark.parametrize("shape", ["lists", "columns", "objects"])
def test_parse_handles_all_response_shapes(data_dir, shape):
    z = make(FakeZetaris(data_dir, shape=shape))
    rows = z.query("SELECT customer_id, name, onboarded FROM kyc WHERE customer_id = ?", ["C9002"])
    assert rows == [{"customer_id": "C9002", "name": "Viktor A. Morozov", "onboarded": date(2026, 3, 29)}]


def test_unparseable_response_gives_actionable_error(data_dir):
    z = make(FakeZetaris(data_dir))
    with pytest.raises(FederationError) as e:
        z.query("SELECT COUNT(*) FROM kyc")  # no alias and no column names in the response
    assert "zetaris_probe.py" in str(e.value)


def test_error_kinds_and_no_secret_leak(data_dir):
    fz = FakeZetaris(data_dir)
    bad = ZetarisFederation(rest_url="http://x/api", api_key="WRONG-SECRET-KEY", org_id="1", client=fz.client())
    with pytest.raises(FederationError) as e:
        bad.query("SELECT 1 AS one")
    assert e.value.kind == "unauthorized" and "WRONG-SECRET-KEY" not in str(e.value)

    def boom(req):
        raise httpx.ConnectError("refused")
    off = ZetarisFederation(rest_url="http://x/api", api_key="k", org_id="1",
                            client=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(FederationError) as e:
        off.query("SELECT 1 AS one")
    assert e.value.kind == "offline" and "docker compose" in str(e.value)

    with pytest.raises(FederationError) as e:
        make(fz).query("SELECT nope FROM kyc")
    assert e.value.kind == "bad_request"


def test_config_validation():
    for kw, kind in [({"rest_url": "", "api_key": "k"}, "config"), ({"rest_url": "http://x", "api_key": ""}, "config"),
                     ({"rest_url": "http://x", "api_key": "k", "org_id": "abc"}, "config")]:
        kw.setdefault("org_id", "1")
        with pytest.raises(FederationError) as e:
            ZetarisFederation(**kw)
        assert e.value.kind == kind


def test_check_reports_tables(data_dir):
    c = make(FakeZetaris(data_dir)).check()
    assert c["reachable"] and c["select1"] and c["tables"]["core_transactions"] > 10000
    assert all(isinstance(v, int) for v in c["tables"].values())


# ---------------------------------------------------------------- the agent, end to end, over the Zetaris adapter
@pytest.mark.parametrize("shape", ["lists", "objects"])
def test_full_benchmark_over_zetaris_matches_labels(data_dir, shape):
    """Every one of the 19 alerts, every SQL statement translated to Spark/Zetaris form and executed through the REST
    contract, with all values arriving as STRINGS, must give the same verdict as the labels."""
    fz = FakeZetaris(data_dir, shape=shape)
    inv = Investigator(fed=make(fz), router=Router([]), memory=None)
    truth = {r["alert_id"]: r["label"] for r in csv.DictReader(open(data_dir / "eval" / "ground_truth.csv"))}
    got = {a["alert_id"]: inv.investigate(a["alert_id"])["verdict"] for a in inv.list_alerts()}
    assert got == truth
    assert len(fz.sqls) > 100  # statements really went over the wire (observed: 172)
    assert all(not re.search(r"\b(core_transactions|core_alerts|core_accounts)\b", s) for s in fz.sqls)  # all rewritten
    assert all("percentile_approx" in s or "quantile_cont" not in s for s in fz.sqls)  # Spark dialect, not DuckDB
