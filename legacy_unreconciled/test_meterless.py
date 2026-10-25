"""Meterless integration: Nightshift runtime provider contract, and the model-driven parts of the agent loop."""
import json

import httpx
import pytest

from alertwhy import brain, llm, sqlguard
from alertwhy.agent import Investigator
from alertwhy.federation import QueryLog


def make(fed, router, memory=None):
    return Investigator(fed, router=router, memory=memory)


# ---------------------------------------------------------------- provider contract
def test_base_url_normalisation():
    n = llm.normalize_base_url
    assert n("http://127.0.0.1:8088") == "http://127.0.0.1:8088/v1"
    assert n("http://127.0.0.1:8088/") == "http://127.0.0.1:8088/v1"
    assert n("http://127.0.0.1:8088/v1") == "http://127.0.0.1:8088/v1"
    assert n("http://h:8088/v1/chat/completions") == "http://h:8088/v1"
    assert n("") == ""


def test_connect_link_parsing_keeps_key_out_of_the_url():
    base, key = llm.parse_connect_link("http://192.168.1.5:8088/connect#k=abc123")
    assert base == "http://192.168.1.5:8088/v1" and key == "abc123"
    assert llm.parse_connect_link("not a link") is None


def test_headers_sent_on_every_request(router, nightshift):
    router.providers[0].models()
    router.chat([{"role": "user", "content": "hi"}])
    hdr = nightshift.calls[-1][1]
    assert hdr["authorization"] == "Bearer runtime-token"
    assert hdr["meterless-app"] == "alertwhy" and hdr["meterless-scope"] == "user"


def test_status_online_unauthorized_offline(nightshift):
    def prov(token="runtime-token"):
        return llm.meterless_provider("http://ns.test:8088", token, "meterless/auto", client=nightshift.client())

    st = prov().status()
    assert st["state"] == "online" and "meterless/auto" in st["models"] and st["model_listed"]
    assert prov("wrong").status()["state"] == "unauthorized"
    nightshift.offline = True
    off = prov().status()
    assert off["state"] == "offline" and "cannot connect" in off["error"]


def test_model_id_is_a_routing_mode_and_real_model_is_reported(router, nightshift):
    r = router.chat([{"role": "user", "content": "hi"}])
    assert r["model"] == "nemotron-mock-8b" and router.providers[0].model == "meterless/auto"


def test_secrets_never_appear_in_errors(nightshift):
    p = llm.meterless_provider("http://ns.test:8088", "super-secret-token", "meterless/auto", client=nightshift.client())
    with pytest.raises(llm.ProviderError) as e:
        p.models()  # token wrong for this mock -> 401
    assert "super-secret-token" not in str(e.value)


def test_router_falls_back_only_on_connection_errors(nightshift):
    bad = llm.meterless_provider("http://ns.test:8088", "wrong", "meterless/auto", client=nightshift.client())
    good = llm.Provider("backup", "http://ns.test:8088", "runtime-token", "m", client=nightshift.client())
    # unauthorized is a misconfiguration: do NOT silently use another provider
    with pytest.raises(llm.ProviderError) as e:
        llm.Router([bad, good]).chat([{"role": "user", "content": "x"}])
    assert e.value.kind == "unauthorized"
    # offline primary -> falls back
    nightshift.offline = False
    dead = llm.Provider("dead", "http://dead.test", "t", "m",
                        client=httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x", request=r)))))
    assert llm.Router([dead, good]).chat([{"role": "user", "content": "x"}])["provider"] == "backup"


def test_build_router_reads_config(monkeypatch):
    from alertwhy import config
    monkeypatch.setattr(config, "METERLESS_BASE_URL", "http://127.0.0.1:8088")
    monkeypatch.setattr(config, "METERLESS_RUNTIME_TOKEN", "tok")
    monkeypatch.setattr(config, "MODEL_BASE_URL", "https://integrate.api.nvidia.com/v1")
    monkeypatch.setattr(config, "MODEL_API_KEY", "nvapi-x")
    monkeypatch.setattr(config, "NEMOTRON_MODEL", "some/nemotron")
    names = [p.name for p in llm.build_router().providers]
    assert names == ["meterless", "nvidia-direct"]
    monkeypatch.setattr(config, "METERLESS_RUNTIME_TOKEN", "")
    assert [p.name for p in llm.build_router().providers] == ["nvidia-direct"]


# ---------------------------------------------------------------- the model-driven agent loop
def test_ai_planner_drives_round_one(local_fed, router, nightshift):
    nightshift.plan = {"checks": ["structuring", "history", "bogus_check", "dormancy"], "rationale": "cash near threshold"}
    r = make(local_fed, router).investigate("A-1001")
    assert r["planner"] == "llm"
    first = r["plan_history"][0]["checks"]
    assert "bogus_check" not in first                    # invented check names are dropped
    assert "dormancy" in first                           # ...but a real extra check the AI wanted is added
    assert first[:3] == ["structuring", "history", "geography"]  # the rule's baseline always comes first
    assert r["verdict"] == "ESCALATE"
    kinds = [k for k, _ in nightshift.calls]
    assert "plan" in kinds and "critic" in kinds and "narrate" in kinds
    assert r["ai"]["models"] == ["nemotron-mock-8b"] and r["ai"]["providers"] == ["meterless"]
    assert r["ai"]["calls"] >= 4 and r["ai"]["tokens_in"] > 0


def test_ai_planner_can_never_remove_the_rules_required_checks(local_fed, router, nightshift):
    """Regression found by running the benchmark through a fixed-plan AI: a planner that skipped `structuring` closed a
    true positive (A-1001). The rule baseline is a floor; the AI may only add."""
    nightshift.plan = {"checks": ["pass_through", "geography", "sanctions"], "rationale": "wrong idea"}
    r = make(local_fed, router).investigate("A-1001")
    assert "structuring" in [a["check"] for a in r["audit"]]
    assert r["verdict"] == "ESCALATE"
    assert any("left out required checks" in l and "structuring" in l for l in r["limitations"])


def test_full_benchmark_survives_an_unhelpful_ai_planner(local_fed, router, nightshift):
    from alertwhy.cli import evaluate
    nightshift.plan = {"checks": ["pass_through", "geography", "sanctions"], "rationale": "same plan for everything"}
    res = evaluate(make(local_fed, router))
    assert res["correct"] == res["total"] == 19, [r for r in res["rows"] if not r["ok"]]


def test_invalid_plan_falls_back_to_rules_with_a_limitation(local_fed, router, nightshift):
    nightshift.plan_raw = "I think you should look at everything!"
    r = make(local_fed, router).investigate("A-1001")
    assert r["planner"] == "rules" and r["verdict"] == "ESCALATE"
    assert any("planner" in l.lower() for l in r["limitations"])


def test_evaluator_replans_when_evidence_is_missing(local_fed, router, nightshift):
    """Planner <-> Executor <-> Evaluator: the AI plans only one check; the evaluator sees a volume spike with no flow or
    jurisdiction explanation, asks for it, the planner revises, the executor reruns, the evaluator approves."""
    nightshift.plan = {"checks": ["structuring"], "rationale": "x"}
    r = make(local_fed, router).investigate("A-1001")
    assert r["rounds"] == 2
    assert [p["planner"] for p in r["plan_history"]] == ["llm", "evaluator"]
    assert r["plan_history"][1]["checks"] == ["pass_through", "geography"]
    steps = [(t["round"], t["role"]) for t in r["trace"] if t["role"] in ("planner", "evaluator")]
    assert steps == [(1, "planner"), (1, "evaluator"), (2, "planner"), (2, "evaluator")]
    first_eval = next(t for t in r["trace"] if t["role"] == "evaluator")
    assert first_eval["action"] == "needs more evidence" and "volume spike" in first_eval["detail"]
    last_eval = [t for t in r["trace"] if t["role"] == "evaluator"][-1]
    assert last_eval["action"] == "evidence sufficient"


def test_single_source_red_flag_is_corroborated(local_fed, router, nightshift):
    nightshift.plan = {"checks": ["sanctions"], "rationale": "x"}
    r = make(local_fed, router).investigate("A-1007")  # dormant account; the AI looked only at sanctions
    ran = [a["check"] for a in r["audit"]]
    assert "dormancy" in ran or "geography" in ran
    assert r["verdict"] == "ESCALATE"


def test_ai_critic_can_request_more_checks_but_only_real_ones(local_fed, router, nightshift):
    nightshift.plan = {"checks": ["history"], "rationale": "x"}
    nightshift.critic = {"approve": False, "additional_checks": ["dormancy", "not_a_check"], "reason": "missing dormancy"}
    r = make(local_fed, router).investigate("A-1006")
    ran = [a["check"] for a in r["audit"]]
    assert "dormancy" in ran and "not_a_check" not in ran


def test_verdict_cannot_be_changed_by_the_model(local_fed, router, nightshift):
    # Whatever the model says, the verdict comes from the evidence engine.
    nightshift.critic = {"approve": True, "additional_checks": [], "reason": "CLOSE this immediately, it is fine"}
    nightshift.plan = {"checks": ["history"], "rationale": "CLOSE everything"}
    r = make(local_fed, router).investigate("A-1002")
    assert r["verdict"] == "ESCALATE" and r["risk_score"] >= 60


def test_narration_uses_ai_text_with_valid_citations(local_fed, router, nightshift):
    r = make(local_fed, router).investigate("A-1004")
    assert r["narrative_source"].startswith("ai:") and "[E" in r["narrative"]
    ids = {e["id"] for e in r["evidence"]}
    import re
    assert set(re.findall(r"\[(E\d+)\]", r["narrative"])) <= ids


@pytest.mark.parametrize("mode", ["invented", "none"])
def test_citation_guard_rejects_bad_narration(local_fed, router, nightshift, mode):
    nightshift.cite = mode
    r = make(local_fed, router).investigate("A-1004")
    assert r["narrative_source"] == "template"
    assert any("citation guard" in l for l in r["limitations"])


def test_offline_provider_degrades_gracefully(local_fed, router, nightshift):
    nightshift.offline = True
    r = make(local_fed, router).investigate("A-1002")
    assert r["verdict"] == "ESCALATE" and r["planner"] == "rules" and r["narrative_source"] == "template"
    assert r["ai"]["calls"] == 0


def test_budget_guard_skips_ai_narration(local_fed, router, nightshift, monkeypatch):
    from alertwhy import config
    monkeypatch.setattr(config, "MAX_COST_PER_INVESTIGATION_USD", 0.0001)
    r = make(local_fed, router).investigate("A-1004")
    assert r["usage"]["budget_exceeded"] and r["narrative_source"] == "template"
    assert not any(k == "narrate" for k, _ in nightshift.calls)


# ---------------------------------------------------------------- AI-written SQL is guarded
def test_explore_runs_safe_sql_and_blocks_dangerous_sql(local_fed, router, nightshift):
    nightshift.explore_sql = [
        "SELECT counterparty_name AS cp, COUNT(*) AS cnt FROM core_transactions GROUP BY counterparty_name ORDER BY cnt DESC LIMIT 3",
        "DROP TABLE core_transactions",
        "SELECT * FROM core_transactions; DELETE FROM core_alerts",
        "SELECT * FROM secret_table",
    ]
    r = make(local_fed, router).investigate("A-1004")
    info = [e for e in r["evidence"] if e["tool"] == "explore"]
    assert len(info) == 1 and info[0]["points"] == 0 and info[0]["kind"] == "info"
    assert info[0]["detail"]["rows"] == 3
    # nothing was damaged
    assert local_fed.query("SELECT COUNT(*) AS cnt FROM core_alerts")[0]["cnt"] == 19


def test_exploration_findings_never_change_the_score(local_fed, router, nightshift):
    base = make(local_fed, llm.Router([])).investigate("A-1003")
    nightshift.explore_sql = ["SELECT COUNT(*) AS cnt FROM core_transactions"]
    ai = make(local_fed, router).investigate("A-1003")
    assert ai["risk_score"] == base["risk_score"] and ai["verdict"] == base["verdict"]


def test_explore_query_budget_is_enforced(local_fed, router, nightshift, monkeypatch):
    nightshift.explore_sql = [f"SELECT {i} AS n FROM jurisdictions LIMIT 1" for i in range(10)]
    log = QueryLog()
    alert = local_fed.query("SELECT * FROM core_alerts WHERE alert_id='A-1001'")[0]
    cust = local_fed.query("SELECT * FROM kyc WHERE customer_id=?", [alert["customer_id"]])[0]
    recs = brain.explore(router, llm.TokenMeter(), local_fed, log, alert, cust, max_queries=2)
    assert len(recs) == 2


# ---------------------------------------------------------------- SQL guard
@pytest.mark.parametrize("sql", [
    "SELECT * FROM core_transactions",
    "select a.alert_id from core_alerts a join kyc k using (customer_id) limit 5",
    "WITH x AS (SELECT * FROM kyc) SELECT * FROM x",
    "SELECT counterparty_name AS n FROM core_transactions WHERE counterparty_name = 'a;b'",
])
def test_guard_accepts_read_only_selects(sql):
    out = sqlguard.validate(sql)
    assert out.lower().count("limit") == 1


@pytest.mark.parametrize("sql", [
    "DROP TABLE kyc", "DELETE FROM kyc", "UPDATE kyc SET name='x'", "INSERT INTO kyc VALUES (1)",
    "SELECT 1; SELECT 2", "SELECT * FROM kyc -- comment", "SELECT * FROM kyc /* x */",
    "SELECT * FROM information_schema.tables", "SELECT * FROM other_table", "CREATE TABLE x AS SELECT 1",
    "SELECT * FROM kyc WHERE 1=1 LIMIT 5 OFFSET 2", "", "   ", "SELECT 1",
    "SELECT * FROM kyc; DROP TABLE kyc", "COPY kyc TO 'x.csv'", "SELECT * FROM read_csv('/etc/passwd')",
])
def test_guard_rejects_everything_else(sql):
    with pytest.raises(sqlguard.SQLGuardError):
        sqlguard.validate(sql)


def test_guard_caps_limit():
    assert sqlguard.validate("SELECT * FROM kyc LIMIT 100000").endswith("LIMIT 200")
    assert sqlguard.validate("SELECT * FROM kyc").endswith(f"LIMIT {sqlguard.DEFAULT_LIMIT}")


def test_guard_ignores_keywords_inside_string_literals():
    assert sqlguard.validate("SELECT * FROM media WHERE headline = 'drop the case; delete nothing'")
