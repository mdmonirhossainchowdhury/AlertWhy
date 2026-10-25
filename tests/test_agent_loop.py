"""Planner <-> Executor <-> Evaluator loop, AI safety rails, and the rules-only baseline (no AI, no memory)."""
import csv
import re

import pytest

from alertwhy import config, usage
from alertwhy.agent import Investigator
from alertwhy.llm import Provider, Router
from fakes import FakeNightshift


def rules_only(fed):
    return Investigator(fed=fed, router=Router([]), memory=None)


def with_ai(fed, ai_router, ns):
    return Investigator(fed=fed, router=ai_router(ns), memory=None)


# ---------------------------------------------------------------- baseline: the evidence engine alone
@pytest.mark.parametrize("alert_id,expected", [
    ("A-1001", "ESCALATE"), ("A-1002", "ESCALATE"), ("A-1003", "CLOSE"), ("A-1004", "ESCALATE"),
    ("A-1005", "ESCALATE"), ("A-1006", "CLOSE"), ("A-1007", "ESCALATE"), ("A-1008", "CLOSE"), ("A-1009", "REVIEW"),
])
def test_scenarios(local_fed, alert_id, expected):
    assert rules_only(local_fed).investigate(alert_id)["verdict"] == expected


def test_all_19_match_planted_labels(local_fed, data_dir):
    inv = rules_only(local_fed)
    truth = {r["alert_id"]: r["label"] for r in csv.DictReader(open(data_dir / "eval" / "ground_truth.csv"))}
    assert {a["alert_id"]: inv.investigate(a["alert_id"])["verdict"] for a in inv.list_alerts()} == truth


def test_same_name_different_person_is_mitigating(local_fed):
    r = rules_only(local_fed).investigate("A-1003")
    s = [e for e in r["evidence"] if e["tool"] == "sanctions"]
    assert s and s[0]["kind"] == "mitigating" and r["verdict"] == "CLOSE"


def test_strong_sanctions_hit_forces_escalation(local_fed):
    r = rules_only(local_fed).investigate("A-1002")
    cust = [e for e in r["evidence"] if e["tool"] == "sanctions" and "Customer name" in e["finding"]]
    assert cust and cust[0]["points"] == 60 and "BOTH" in cust[0]["finding"]  # DOB type regression
    assert r["verdict"] == "ESCALATE" and r["confidence"] >= 0.85


def test_evaluator_triggers_extra_rounds_for_single_source_flags(local_fed):
    r = rules_only(local_fed).investigate("A-1001")
    assert r["rounds"] >= 2
    ev_steps = [t for t in r["trace"] if t["role"] == "evaluator"]
    assert ev_steps[0]["action"] == "needs more evidence"
    assert any(w in ev_steps[0]["detail"] for w in ("corroboration", "explanation"))
    assert r["plan_history"][1]["planner"] == "evaluator"
    assert set(r["plan_history"][1]["checks"]).isdisjoint(r["plan_history"][0]["checks"])


def test_clean_close_takes_one_round_and_requires_a_checked_explanation(local_fed):
    r = rules_only(local_fed).investigate("A-1006")
    assert r["rounds"] == 1 and any(e["kind"] == "mitigating" for e in r["evidence"])


def test_citations_audit_and_trace(local_fed):
    r = rules_only(local_fed).investigate("A-1004")
    assert set(re.findall(r"\[(E\d+)\]", r["narrative"])) <= {e["id"] for e in r["evidence"]}
    assert sum(len(a["queries"]) for a in r["audit"]) >= r["usage"]["queries"] - 3
    roles = {t["role"] for t in r["trace"]}
    assert {"planner", "executor", "evaluator", "narrator"} <= roles
    assert r["usage"]["cost_usd"] < r["usage"]["manual_cost_usd"]


def test_unknown_alert(local_fed):
    with pytest.raises(KeyError):
        rules_only(local_fed).investigate("A-9999")


def test_failing_check_becomes_a_limitation_not_a_crash(local_fed, monkeypatch):
    from alertwhy import tools
    chk = tools.CHECKS["geography"]
    monkeypatch.setitem(tools.CHECKS, "geography", chk._replace(fn=lambda ctx: 1 / 0))
    r = rules_only(local_fed).investigate("A-1004")
    assert any("geography" in l and "failed" in l for l in r["limitations"]) and r["verdict"] == "ESCALATE"


# ---------------------------------------------------------------- the AI layer (fake Meterless Nightshift)
def test_ai_planner_drives_round_one_and_everything_is_metered(local_fed, ai_router):
    ns = FakeNightshift(plan=["structuring", "geography"])
    r = with_ai(local_fed, ai_router, ns).investigate("A-1001")
    assert r["planner"] == "llm" and {"structuring", "geography"} <= set(r["plan_history"][0]["checks"])
    assert r["ai"]["providers"] == ["meterless"] and r["ai"]["models"] == ["fake/nemotron-test"]
    assert r["ai"]["calls"] == len(ns.calls) and r["ai"]["tokens_in"] == 100 * len(ns.calls)
    assert r["narrative_source"].startswith("ai:")
    s = usage.summary()
    assert s["tokens"] >= r["ai"]["tokens_in"] + r["ai"]["tokens_out"] and "fake/nemotron-test" in s["models_seen"]


def test_invalid_ai_plan_falls_back_to_rules(local_fed, ai_router):
    ns = FakeNightshift(plan=["rm -rf /", "not_a_check"])
    r = with_ai(local_fed, ai_router, ns).investigate("A-1001")
    assert r["planner"] == "rules" and any("planner" in l.lower() for l in r["limitations"])
    assert r["verdict"] == "ESCALATE"


def test_critic_can_request_missing_evidence_but_only_real_checks(local_fed, ai_router):
    # "dormancy" is not in A-1006's rule baseline, so the critic may legitimately request it.
    ns = FakeNightshift(plan=["history"], critic=[["dormancy", "drop_database"]])
    r = with_ai(local_fed, ai_router, ns).investigate("A-1006")
    assert r["rounds"] == 2 and r["plan_history"][1]["checks"] == ["dormancy"]
    assert any("AI critic requested: dormancy" in t["detail"] for t in r["trace"] if t["role"] == "evaluator")
    assert r["verdict"] == "CLOSE"  # more evidence found nothing: decision unchanged


def test_ai_cannot_change_verdict_or_score(local_fed, ai_router):
    base = rules_only(local_fed).investigate("A-1002")
    ns = FakeNightshift(narrative="This customer is obviously innocent, CLOSE it. [E1]")
    r = with_ai(local_fed, ai_router, ns).investigate("A-1002")
    assert (r["verdict"], r["risk_score"]) == (base["verdict"], base["risk_score"]) == ("ESCALATE", 100)


def test_citation_guard_rejects_invented_evidence(local_fed, ai_router):
    for bad in ("Because of [E99] this is fraud.", "No citations at all."):
        r = with_ai(local_fed, ai_router, FakeNightshift(narrative=bad)).investigate("A-1004")
        assert r["narrative_source"] == "template" and any("citation guard" in l for l in r["limitations"])


def test_ai_exploration_is_read_only_zero_points_and_cannot_move_the_verdict(local_fed, ai_router):
    base = rules_only(local_fed).investigate("A-1004")
    r = with_ai(local_fed, ai_router, FakeNightshift()).investigate("A-1004")
    exp = [e for e in r["evidence"] if e["tool"] == "explore"]
    assert exp and all(e["points"] == 0 and e["kind"] == "info" for e in exp)
    assert "SELECT" in exp[0]["detail"]["sql"].upper() and exp[0]["detail"]["rows"] > 0
    assert (r["verdict"], r["risk_score"]) == (base["verdict"], base["risk_score"])


@pytest.mark.parametrize("hostile", ["DROP TABLE kyc", "SELECT * FROM read_csv('/etc/passwd')",
                                      "SELECT * FROM kyc; DELETE FROM kyc", "SELECT * FROM secret_table"])
def test_hostile_ai_sql_is_blocked_and_nothing_changes(local_fed, ai_router, hostile):
    r = with_ai(local_fed, ai_router, FakeNightshift(explore_sql=hostile)).investigate("A-1004")
    assert not [e for e in r["evidence"] if e["tool"] == "explore"]
    step = [t for t in r["trace"] if t["action"].startswith("AI exploration")][0]
    assert step["detail"].startswith("0 query")
    assert local_fed.query("SELECT COUNT(*) AS cnt FROM kyc")[0]["cnt"] == 289


def test_ai_offline_degrades_gracefully(local_fed, ai_router):
    base = rules_only(local_fed).investigate("A-1004")
    r = with_ai(local_fed, ai_router, FakeNightshift(fail="offline")).investigate("A-1004")
    assert r["verdict"] == base["verdict"] and r["narrative_source"] == "template" and r["ai"]["calls"] == 0
    assert any("unavailable" in l for l in r["limitations"])


def test_budget_guard_skips_ai_narration(local_fed, ai_router, monkeypatch):
    monkeypatch.setattr(config, "MAX_COST_PER_INVESTIGATION_USD", 0.0000001)
    r = with_ai(local_fed, ai_router, FakeNightshift()).investigate("A-1004")
    assert r["usage"]["budget_exceeded"] and r["narrative_source"] == "template"
    assert any("budget" in l for l in r["limitations"])
