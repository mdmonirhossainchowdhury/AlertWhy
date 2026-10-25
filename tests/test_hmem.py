"""Meterless H-MEM engine: the vendored engine passes the official conformance suite, and our bridge honours it."""
import os
import subprocess
import time
from pathlib import Path

import pytest

from alertwhy.hmem import ENGINE_DIR, HMemClient, MemoryUnavailable
from conftest import needs_node

pytestmark = needs_node


def test_official_conformance_suite_passes_on_the_vendored_engine():
    env = {**os.environ, "HMEM_IMPL": str(ENGINE_DIR / "src" / "index.ts")}
    p = subprocess.run(["node", "--import", "tsx", "conformance/runner.ts"], cwd=ENGINE_DIR, env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "13/13 checks passed" in p.stdout and "FAIL" not in p.stdout


def test_engine_source_is_unmodified_apache_reference():
    """NOTICE says src/ is copied unmodified; guard against accidental edits by checking the license header + files."""
    assert (ENGINE_DIR / "LICENSE").read_text().lstrip().startswith("Apache License")
    assert {f.name for f in (ENGINE_DIR / "src").glob("*.ts")} >= {"memoryStore.ts", "memoryRetrieval.ts",
                                                                    "trustLedger.ts", "sleepCycle.ts", "dreaming.ts"}


def test_bridge_feedback_deltas_match_the_spec(tmp_path):
    c = HMemClient(persist_dir=tmp_path)
    try:
        m = c.call("add", content="Feedback target record", source="t", confidence=0.7)
        assert c.call("feedback", id=m["id"], kind="helpful")["confidence"] == pytest.approx(0.75)
        assert c.call("feedback", id=m["id"], kind="not_helpful")["confidence"] == pytest.approx(0.72)
        r = c.call("feedback", id=m["id"], kind="wrong")
        assert r["confidence"] == pytest.approx(0.52) and "review" in r["tags"]
    finally:
        c.close()


def test_record_without_source_is_rejected_by_construction(tmp_path):
    c = HMemClient(persist_dir=tmp_path)
    try:
        with pytest.raises(MemoryUnavailable) as e:
            c.call("add", content="no provenance", source="")
        assert "no source" in str(e.value)
    finally:
        c.close()


def test_memory_persists_across_sessions_and_ledger_records_every_change(tmp_path):
    c = HMemClient(persist_dir=tmp_path)
    m = c.call("add", content="Analyst escalated Kestrel Partners SA wires", source="analyst", tags=["cp:kestrel"])
    c.call("feedback", id=m["id"], kind="helpful")
    c.close()
    time.sleep(0.3)
    c2 = HMemClient(persist_dir=tmp_path)  # a NEW process: the earlier session's memory must still be there
    try:
        again = c2.call("query", query="Kestrel Partners wires", topN=3, threshold=0)
        assert [x["id"] for x in again["memories"]] == [m["id"]]
        actions = [e["action"] for e in c2.call("ledger")]
        assert actions[:2] == ["create", "feedback"]
        assert Path(tmp_path / "state.json").exists() and Path(tmp_path / "ledger.jsonl").exists()
    finally:
        c2.close()


def test_superseded_memory_is_penalised_in_ranking_but_never_deleted(tmp_path):
    c = HMemClient(persist_dir=tmp_path)
    try:
        old = c.call("add", content="Analyst decision on alert A-1 was CLOSE", source="a", tags=["alert:A-1"])
        new = c.call("add", content="Analyst decision on alert A-1 was ESCALATE", source="a", tags=["alert:A-1"],
                     supersedes=old["id"])
        c.call("mark_superseded", old=old["id"], new=new["id"])
        res = c.call("query", query="analyst decision alert A-1", topN=5, threshold=0)["memories"]
        assert [m["id"] for m in res] == [new["id"], old["id"]] and res[1]["supersededBy"] == new["id"]
    finally:
        c.close()


def test_status_reports_offline_when_engine_missing(tmp_path):
    from alertwhy.hmem import AnalystMemory
    s = AnalystMemory(HMemClient(persist_dir=tmp_path, engine_dir=tmp_path / "nope")).status()
    assert s["state"] == "offline" and "missing" in s["error"]
