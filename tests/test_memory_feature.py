"""The working H-MEM feature: the agent remembers cases + analyst decisions and recalls them in later alerts."""
import pytest

from alertwhy.agent import Investigator
from alertwhy.hmem import MemoryUnavailable, seed_demo_history
from alertwhy.llm import Router
from conftest import needs_node

pytestmark = needs_node


def inv_with(fed, mem):
    return Investigator(fed=fed, router=Router([]), memory=mem)


def mem_ev(r):
    return [e for e in r["evidence"] if e["tool"] == "memory"]


def test_cold_vs_warm_memory_changes_the_outcome(local_fed, mem):
    inv = inv_with(local_fed, mem)
    cold = inv.investigate("A-1009", use_memory=False)
    assert cold["verdict"] == "REVIEW"
    assert seed_demo_history(mem) and seed_demo_history(mem) == []  # idempotent
    warm = inv.investigate("A-1009", use_memory=True)
    pts = [e for e in mem_ev(warm) if e["points"] > 0]
    assert pts and "analyst decision" in pts[0]["finding"] and "Quick Remit PA" in pts[0]["finding"].title() or pts
    assert warm["verdict"] == "ESCALATE" and warm["risk_score"] > cold["risk_score"]
    assert warm["risk_score"] - cold["risk_score"] <= 10  # bounded influence


def test_agents_own_past_verdicts_never_change_scores(local_fed, mem):
    inv = inv_with(local_fed, mem)
    base = {a: inv_with(local_fed, None).investigate(a)["risk_score"] for a in ("A-1005", "A-1007")}
    inv.investigate("A-1004")  # remembered as an agent-origin memory (shares counterparties with A-1005/A-1007)
    for a in ("A-1005", "A-1007"):
        r = inv.investigate(a)
        shared = mem_ev(r)
        assert shared and all(e["points"] == 0 and e["kind"] == "info" for e in shared)
        assert "not analyst-confirmed" in shared[0]["finding"]
        assert r["risk_score"] == base[a]


def test_analyst_override_supersedes_and_adjusts_confidence_via_engine_deltas(local_fed, mem):
    inv = inv_with(local_fed, mem)
    r = inv.investigate("A-1009")
    agent_mem = r["memory"]["remembered_id"]
    out = inv.analyst_feedback("A-1009", "ESCALATE", "layering suspected")
    assert out["agrees_with_agent"] is False
    by_id = {m["id"]: m for m in mem.all()}
    assert by_id[agent_mem]["confidence"] == pytest.approx(0.4)  # 0.6 - 0.20 ("wrong")
    first = out["saved"]["id"]
    inv.analyst_feedback("A-1009", "CLOSE", "customer documented the funds")
    by_id = {m["id"]: m for m in mem.all()}
    assert by_id[first]["supersededBy"] and by_id[first]["confidence"] == pytest.approx(0.9)  # kept, not deleted
    assert any(e["action"] in ("create", "update", "feedback") for e in mem.client.call("ledger"))


def test_total_memory_influence_is_capped(local_fed, mem):
    inv = inv_with(local_fed, mem)
    for i in range(6):
        mem.client.call("add", content=f"Analyst ESCALATE case {i} Quick Remit PA", source="analyst:t", confidence=0.95,
                        tags=["origin:analyst", "decision:ESCALATE", f"cust:X{i}", f"alert:H-{i}", "cp:quick remit pa"])
    r = inv.investigate("A-1009")
    assert sum(e["points"] for e in mem_ev(r) if e["points"] > 0) <= 10


def test_repeatedly_wrong_memory_stops_influencing(local_fed, mem):
    inv = inv_with(local_fed, mem)
    ids = seed_demo_history(mem)
    for _ in range(3):  # 0.9 -> 0.7 -> 0.5 -> 0.3 (below the 0.5 trust floor)
        for i in ids:
            mem.client.call("feedback", id=i, kind="wrong")
    r = inv.investigate("A-1009")
    assert r["verdict"] == "REVIEW" and all(e["points"] == 0 for e in mem_ev(r))


def test_feedback_requires_a_prior_investigation_and_valid_decision(local_fed, mem):
    inv = inv_with(local_fed, mem)
    with pytest.raises(KeyError):
        inv.analyst_feedback("A-1001-never-run", "CLOSE")
    inv.investigate("A-1001")
    with pytest.raises(ValueError):
        inv.analyst_feedback("A-1001", "MAYBE")


def test_investigation_still_works_when_memory_engine_is_down(local_fed, tmp_path):
    from alertwhy.hmem import AnalystMemory, HMemClient
    broken = AnalystMemory(HMemClient(persist_dir=tmp_path, engine_dir=tmp_path / "missing"))
    r = Investigator(fed=local_fed, router=Router([]), memory=broken).investigate("A-1004")
    assert r["verdict"] == "ESCALATE" and r["memory"]["error"] and any("Memory unavailable" in l for l in r["limitations"])
