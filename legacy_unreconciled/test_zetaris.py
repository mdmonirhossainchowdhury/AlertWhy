"""Zetaris integration: REST contract, SQL translation, error handling, and the full agent over the REST adapter."""
import json
import re
import uuid

import pytest

from alertwhy import zsetup
from alertwhy.agent import Investigator
from alertwhy.cli import evaluate
from alertwhy.federation import (FederationError, QueryLog, ZetarisFederation, bind, coerce, derive_columns, literal,
                                 rewrite_tables, DEFAULT_TABLE_MAP)


# ---------------------------------------------------------------- the documented REST contract
def test_request_follows_the_setup_guide_contract(zfed, zetaris):
    rows = zfed.query("SELECT 1 AS one")  # no table: still must be accepted as a plain statement
    # SELECT 1 has no logical table, the mock executes it as-is
    headers, body = zetaris.requests[-1]
    assert headers["authorization"] == "Bearer test-key"
    assert headers["x-org-id"] == "1"
    uuid.UUID(headers["x-request-id"])
    uuid.UUID(body["queryId"])
    assert body["source"] == "SqlEditor" and body["limit"] > 0 and body["sql"] == "SELECT 1 AS one"
    assert rows == [{"one": "1"}]  # unknown column type stays a string; the guide's own test compares str()


def test_every_request_gets_a_fresh_request_id(zfed, zetaris):
    for _ in range(3):
        zfed.query("SELECT 1 AS one")
    ids = {h["x-request-id"] for h, _ in zetaris.requests}
    assert len(ids) == 3


def test_logical_names_are_rewritten_to_datasource_tables(zfed, zetaris):
    zfed.query("SELECT COUNT(*) AS cnt FROM core_transactions")
    assert "core_pg.transactions" in zetaris.statements[-1]
    assert "FROM core_transactions" not in zetaris.statements[-1]


def test_join_across_datasources(zfed, zetaris):
    rows = zfed.query("SELECT a.alert_id, k.name AS customer_name FROM core_alerts a JOIN kyc k USING (customer_id) "
                      "ORDER BY a.alert_id")
    assert len(rows) == 19 and rows[0]["alert_id"] == "A-1001" and rows[0]["customer_name"]
    s = zetaris.statements[-1]
    assert "core_pg.alerts" in s and "kyc_pg.customers" in s


def test_spark_dialect_used_for_percentiles(zfed, zetaris, data_dir):
    # DuckDB-only quantile_cont would be rejected by the mock exactly like Spark would
    from alertwhy.tools import Ctx, history_consistency
    log = QueryLog()
    alert = zfed.query("SELECT * FROM core_alerts WHERE alert_id=?", ["A-1006"], log)[0]
    cust = zfed.query("SELECT * FROM kyc WHERE customer_id=?", [alert["customer_id"]], log)[0]
    items = history_consistency(Ctx(zfed, log, alert, cust))
    assert items and items[0]["kind"] == "mitigating"
    assert any("percentile_approx" in s for s in zetaris.statements)
    assert not any("quantile_cont" in s for s in zetaris.statements)


def test_types_are_coerced_from_string_rows(zfed):
    t = zfed.query("SELECT * FROM core_transactions WHERE txn_id=?", ["T000001"])[0]
    assert isinstance(t["amount_usd"], float) and t["ts"].year >= 2025 and t["ts"].tzinfo is None
    k = zfed.query("SELECT * FROM kyc WHERE customer_id=?", ["C0001"])[0]
    assert k["onboarded"].year >= 2015 and isinstance(k["expected_monthly_usd"], float)


def test_column_names_from_response_when_present(data_dir):
    from mocks import MockZetaris
    z = MockZetaris(data_dir, with_columns=True)
    fed = ZetarisFederation("http://z.test/api/v1.0", "test-key", "1", client=z.client())
    # SELECT * on a join cannot be derived from the SQL text; the response's own column list must be used
    rows = fed.query("SELECT * FROM core_alerts a JOIN kyc k USING (customer_id) LIMIT 2")
    assert rows and "alert_id" in rows[0] and "name" in rows[0]


def test_unnameable_columns_raise_a_helpful_error(zfed):
    with pytest.raises(FederationError) as e:
        zfed.query("SELECT * FROM core_alerts a JOIN kyc k USING (customer_id) LIMIT 2")
    assert "zetaris_probe" in str(e.value)


# ---------------------------------------------------------------- errors a participant will actually hit
def test_401_message_tells_you_to_recreate_the_key(zetaris):
    fed = ZetarisFederation("http://z.test/api/v1.0", "WRONG", "1", client=zetaris.client())
    with pytest.raises(FederationError) as e:
        fed.query("SELECT 1 AS one")
    assert e.value.kind == "unauthorized" and "API key" in str(e.value)


def test_400_for_bad_org_id(zetaris):
    fed = ZetarisFederation("http://z.test/api/v1.0", "test-key", "7", client=zetaris.client())
    with pytest.raises(FederationError) as e:
        fed.query("SELECT 1 AS one")
    assert e.value.kind == "bad_request" and "ORG" in str(e.value).upper()


def test_offline_message_mentions_docker(zetaris):
    import httpx

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    fed = ZetarisFederation("http://z.test/api/v1.0", "k", "1", client=httpx.Client(transport=httpx.MockTransport(refuse)))
    with pytest.raises(FederationError) as e:
        fed.query("SELECT 1 AS one")
    assert e.value.kind == "offline" and "docker compose" in str(e.value)


def test_missing_configuration_is_explicit():
    with pytest.raises(FederationError, match="ZETARIS_REST_URL"):
        ZetarisFederation("", "k", "1")
    with pytest.raises(FederationError, match="ZETARIS_API_KEY"):
        ZetarisFederation("http://x/api/v1.0", "", "1")
    with pytest.raises(FederationError, match="numeric"):
        ZetarisFederation("http://x/api/v1.0", "k", "abc")


def test_check_reports_select1_and_all_seven_tables(zfed):
    c = zfed.check()
    assert c["reachable"] and c["select1"] and c["error"] is None
    assert set(c["tables"]) == {"core_accounts", "core_transactions", "core_alerts", "kyc", "sanctions", "media",
                                "jurisdictions"}
    assert all(isinstance(v, int) and v > 0 for v in c["tables"].values())


# ---------------------------------------------------------------- SQL text handling (injection safety)
def test_bind_escapes_quotes_and_backslashes():
    sql = bind("SELECT * FROM kyc WHERE name = ? AND customer_id = ?", ["O'Brien\\x", "C1"])
    assert sql == "SELECT * FROM kyc WHERE name = 'O\\'Brien\\\\x' AND customer_id = 'C1'"


def test_bind_rejects_control_characters_and_count_mismatch():
    with pytest.raises(FederationError):
        bind("SELECT ?", ["a\nb"])
    with pytest.raises(FederationError):
        bind("SELECT ?, ?", [1])
    with pytest.raises(FederationError):
        bind("SELECT ?", [1, 2])


def test_question_marks_inside_string_literals_are_not_parameters():
    assert bind("SELECT 'what?' AS q, ?", [5]) == "SELECT 'what?' AS q, 5"


def test_rewrite_leaves_string_literals_alone():
    out = rewrite_tables("SELECT 'from kyc' AS s FROM kyc WHERE x='join media'", DEFAULT_TABLE_MAP)
    assert out == "SELECT 'from kyc' AS s FROM kyc_pg.customers WHERE x='join media'"


def test_literal_types():
    from datetime import date, datetime
    assert literal(None) == "NULL" and literal(True) == "TRUE" and literal(3) == "3"
    assert literal(datetime(2026, 1, 2, 3, 4, 5)) == "TIMESTAMP '2026-01-02 03:04:05'"
    assert literal(date(2026, 1, 2)) == "DATE '2026-01-02'"
    with pytest.raises(FederationError):
        literal(float("nan"))


def test_derive_columns():
    assert derive_columns("SELECT a, b AS c, SUM(x) AS total_usd FROM core_alerts") == ["a", "c", "total_usd"]
    assert derive_columns("SELECT * FROM kyc", DEFAULT_TABLE_MAP)[0] == "customer_id"
    assert derive_columns("SELECT SUM(x) FROM core_alerts") is None


def test_coerce_variants():
    assert coerce("ts", "2026-10-10T15:07:00.000+0000").tzinfo is None
    assert coerce("amount_usd", "12.50") == 12.5 and coerce("cnt", "7") == 7
    assert coerce("name", "7") == "7"


# ---------------------------------------------------------------- the whole agent over the REST adapter
def test_agent_over_zetaris_matches_local_backend_on_all_alerts(zfed, local_fed, data_dir):
    via_rest = evaluate(Investigator(zfed, router=__import__("alertwhy.llm", fromlist=["Router"]).Router([]), memory=None))
    via_local = evaluate(Investigator(local_fed, router=__import__("alertwhy.llm", fromlist=["Router"]).Router([]),
                                      memory=None))
    assert via_rest["correct"] == via_rest["total"] == 19, via_rest["missed_escalations"]
    assert [(r["alert_id"], r["got"], r["score"]) for r in via_rest["rows"]] == \
           [(r["alert_id"], r["got"], r["score"]) for r in via_local["rows"]]


def test_audit_trail_records_the_native_zetaris_sql(zfed, zetaris):
    from alertwhy.llm import Router
    r = Investigator(zfed, router=Router([]), memory=None).investigate("A-1004")
    sqls = [q["sql"] for a in r["audit"] for q in a["queries"]]
    assert sqls and all(re.search(r"core_pg|kyc_pg|screen_pg", s) for s in sqls if "FROM" in s.upper())
    assert r["federation_backend"].startswith("Zetaris")


# ---------------------------------------------------------------- setup helpers
def test_register_statements_are_exact_and_maskable():
    st = zsetup.register_statements("host.docker.internal", 5433, "alertwhy", "s3cret")
    assert len(st) == 6
    assert st[0].startswith("CREATE DATASOURCE core_pg DESCRIBE BY")
    assert 'jdbcdriver "org.postgresql.Driver"' in st[0]
    assert 'jdbcurl "jdbc:postgresql://host.docker.internal:5433/core"' in st[0]
    assert st[1] == "REGISTER DATASOURCE TABLES FROM core_pg"
    masked = zsetup.register_statements("h", 5433, "u", "s3cret", mask=True)
    assert "s3cret" not in " ".join(masked) and "********" in masked[0]
    assert not any(s.rstrip().endswith(";") for s in st)  # one statement per call, no trailing semicolon


def test_unsafe_option_values_are_refused():
    with pytest.raises(ValueError):
        zsetup.register_statements("h", 5433, "u", 'pa"ss')
    with pytest.raises(ValueError):
        zsetup._ident("x; DROP TABLE y")


def test_expected_counts_cover_every_logical_table(data_dir):
    c = zsetup.expected_counts(data_dir)
    assert set(c) == set(DEFAULT_TABLE_MAP) and c["core_alerts"] == 19


def test_verify_against_mock_zetaris(zfed, data_dir):
    ok, res = zsetup.verify(zfed, data_dir)
    assert ok and all(a == b for a, b in res.values())
