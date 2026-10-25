"""Meterless H-MEM engine integration.

`HMemClient` talks to the vendored, unmodified Meterless H-MEM engine (engines/hmem, Apache-2.0) through a
JSON-lines bridge (engines/hmem/bridge.ts) running under Node.

`AnalystMemory` is the working feature built on it: the agent REMEMBERS cases and analyst decisions across sessions
and RECALLS them when a later alert involves the same customer or counterparty.

Safeguards (so memory helps without becoming a bias machine):
  * only ANALYST decisions move the score; the agent's own past verdicts are shown as info, never as evidence
    (no self-reinforcement loop);
  * memory contributes at most +10 risk / -10 mitigation in total, scaled by the memory's confidence;
  * analyst feedback adjusts confidence through the engine's own deltas (helpful +0.05, not_helpful -0.03,
    wrong -0.20); a memory pushed below 0.5 stops influencing scores;
  * an overturned decision is superseded (penalised in ranking, never deleted) and every change is in the ledger.
"""
import json
import os
import queue
import subprocess
import threading
from pathlib import Path

from . import config
from .tools import ev, norm_name

ENGINE_DIR = Path(__file__).resolve().parent.parent / "engines" / "hmem"


class MemoryUnavailable(RuntimeError):
    pass


class HMemClient:
    def __init__(self, persist_dir=None, node_bin=None, engine_dir=None):
        self.engine_dir = Path(engine_dir or ENGINE_DIR)
        self.persist_dir = str(persist_dir or config.HMEM_DIR)
        self.node_bin = node_bin or config.NODE_BIN
        self._proc = None
        self._q = queue.Queue()
        self._lock = threading.Lock()
        self._n = 0

    def _command(self):
        tsx = self.engine_dir / "node_modules" / ".bin" / ("tsx.cmd" if os.name == "nt" else "tsx")
        if tsx.exists():
            return [str(tsx), "bridge.ts"]
        return ["npx", "--yes", "tsx", "bridge.ts"]

    def start(self):
        if self._proc and self._proc.poll() is None:
            return
        if not (self.engine_dir / "bridge.ts").exists():
            raise MemoryUnavailable("H-MEM engine folder is missing (engines/hmem)")
        env = {**os.environ, "HMEM_DIR": self.persist_dir}
        try:
            self._proc = subprocess.Popen(self._command(), cwd=self.engine_dir, env=env, text=True, bufsize=1,
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            raise MemoryUnavailable(f"cannot start H-MEM (is Node.js 18+ installed? run `npm install` in "
                                    f"engines/hmem): {e}")
        self._q = queue.Queue()
        threading.Thread(target=self._reader, args=(self._proc, self._q), daemon=True).start()

    @staticmethod
    def _reader(proc, q):
        for line in proc.stdout:
            try:
                q.put(json.loads(line))
            except ValueError:
                pass
        q.put(None)

    def call(self, op, timeout=30, **args):
        with self._lock:
            self.start()
            self._n += 1
            rid = self._n
            try:
                self._proc.stdin.write(json.dumps({"id": rid, "op": op, "args": args}) + "\n")
                self._proc.stdin.flush()
            except (OSError, ValueError):
                raise MemoryUnavailable("H-MEM bridge process is not running")
            while True:
                try:
                    msg = self._q.get(timeout=timeout)
                except queue.Empty:
                    raise MemoryUnavailable("H-MEM bridge timed out")
                if msg is None:
                    raise MemoryUnavailable("H-MEM bridge exited unexpectedly (run `npm install` in engines/hmem)")
                if msg.get("id") == rid:
                    if not msg.get("ok"):
                        raise MemoryUnavailable(f"H-MEM error: {msg.get('error')}")
                    return msg["result"]

    def reset(self):
        """Stop the bridge and delete the persisted memory (state.json + ledger.jsonl)."""
        import shutil
        self.close()
        self._proc = None
        shutil.rmtree(self.persist_dir, ignore_errors=True)

    def close(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()


def _tags_of(m, prefix):
    return {t.split(":", 1)[1] for t in m["tags"] if t.startswith(prefix + ":")}


class AnalystMemory:
    CAP = 10

    def __init__(self, client=None):
        self.client = client or HMemClient()

    # ---- status
    def status(self):
        try:
            r = self.client.call("ping", timeout=20)
            return {"state": "online", "records": r["records"], "error": None}
        except MemoryUnavailable as e:
            return {"state": "offline", "records": 0, "error": str(e)}

    def all(self):
        return self.client.call("all")

    def reset(self):
        """Delete all persisted memory (used by the UI's reset button and tests)."""
        self.client.reset()

    # ---- recall -> evidence
    def recall(self, alert, customer, counterparties):
        """Return evidence items (tool='memory') for earlier cases touching this customer or counterparties."""
        cps = {norm_name(c) for c in counterparties if c}
        cid = customer["customer_id"]
        text = f"{customer['name']} {cid} {alert['rule_id']} " + " ".join(sorted(cps))
        res = self.client.call("query", query=text, topN=20, threshold=0)
        used, out = {"risk": 0, "mitigating": 0}, []
        for m in sorted(res["memories"], key=lambda x: -x["relevance"]):
            if m.get("supersededBy"):
                continue
            shared_cp = sorted(_tags_of(m, "cp") & cps)
            same_cust = cid in _tags_of(m, "cust")
            if not (shared_cp or same_cust):
                continue
            if alert["alert_id"] in _tags_of(m, "alert"):
                continue  # the case's own earlier record is not evidence about itself
            origin = (_tags_of(m, "origin") or {"agent"}).pop()
            decision = (_tags_of(m, "decision") or {"?"}).pop()
            prior_alert = (_tags_of(m, "alert") or {"?"}).pop()
            why = ("same counterparty " + ", ".join(shared_cp)) if shared_cp else "same customer"
            base, kind = 0, "info"
            if origin == "analyst" and m["confidence"] >= 0.5:
                if decision == "ESCALATE":
                    base, kind = (10 if shared_cp else 6), "risk"
                elif decision == "CLOSE" and same_cust:
                    base, kind = -8, "mitigating"
            pts = round(base * m["confidence"])
            if pts and kind != "info":
                room = self.CAP - used[kind]
                pts = min(abs(pts), max(room, 0)) * (1 if pts > 0 else -1)
                used[kind] += abs(pts)
            if pts == 0:
                kind = "info"
            label = {"analyst": "analyst decision", "agent": "earlier automated investigation"}.get(origin, origin)
            out.append(ev("memory", "h-mem", kind, pts,
                          f"Memory {m['id']}: {label} on alert {prior_alert} ({decision}) - {why}. "
                          f"Memory confidence {m['confidence']:.2f}"
                          + ("" if origin == "analyst" else "; not analyst-confirmed, so it does not change the score")
                          + ".", memory_id=m["id"], origin=origin, decision=decision, relevance=round(m["relevance"], 3),
                          shared=shared_cp))
            if len(out) >= 4:
                break
        return out

    # ---- remember
    def remember_investigation(self, alert, customer, verdict, score, confidence, counterparties, entities):
        tags = ["origin:agent", f"decision:{verdict}", f"cust:{customer['customer_id']}",
                f"alert:{alert['alert_id']}", f"rule:{alert['rule_id']}"]
        tags += [f"cp:{norm_name(c)}" for c in counterparties if c]
        tags += [f"ent:{e}" for e in entities]
        return self.client.call(
            "add", content=(f"Automated investigation of alert {alert['alert_id']} ({alert['rule_id']}) for "
                            f"{customer['name']} ({customer['customer_id']}): {verdict}, risk {score}/100, "
                            f"confidence {confidence}. Counterparties: {', '.join(counterparties[:6]) or 'none'}."),
            source="agent:alertwhy", confidence=0.6, tags=tags, layer="working")

    def record_analyst_decision(self, alert, customer, decision, agent_verdict, counterparties, entities,
                                note="", agent_memory_id=None, used_memory_ids=()):
        existing = [m for m in self.all() if f"alert:{alert['alert_id']}" in m["tags"]
                    and "origin:analyst" in m["tags"] and not m.get("supersededBy")]
        tags = ["origin:analyst", f"decision:{decision}", f"cust:{customer['customer_id']}",
                f"alert:{alert['alert_id']}", f"rule:{alert['rule_id']}"]
        tags += [f"cp:{norm_name(c)}" for c in counterparties if c]
        tags += [f"ent:{e}" for e in entities]
        content = (f"Analyst decided {decision} on alert {alert['alert_id']} ({alert['rule_id']}) for "
                   f"{customer['name']} ({customer['customer_id']}); the agent had recommended {agent_verdict}. "
                   f"Counterparties: {', '.join(counterparties[:6]) or 'none'}. {note}".strip())
        prior = existing[0]["id"] if existing else None
        rec = self.client.call("add", content=content, source="analyst:alertwhy-ui", confidence=0.9, tags=tags,
                               layer="long_term", supersedes=prior)
        if prior:
            self.client.call("mark_superseded", old=prior, new=rec["id"])
        agree = decision == agent_verdict
        if agent_memory_id:
            self.client.call("feedback", id=agent_memory_id, kind="helpful" if agree else "wrong")
        for mid in used_memory_ids:
            self.client.call("feedback", id=mid, kind="helpful" if agree else "not_helpful")
        return rec


# ---------------------------------------------------------------------------------------------------------------
# DEMO SEED. Clearly synthetic "past analyst decisions" so the cold-vs-warm memory effect can be shown in a demo.
# They are labelled source="analyst:seed-demo" and the UI says so. They never touch the benchmark (eval runs with
# memory off).
SEED_CASES = [
    {"alert": "H-0412", "cust": "HIST-0412", "rule": "NEW_ACCOUNT_ACTIVITY", "decision": "ESCALATE",
     "cps": ["Quick Remit PA", "Lotus Global Ltd"],
     "note": "Rapid remittances routed through Quick Remit PA after onboarding; SAR filed."},
    {"alert": "H-0377", "cust": "HIST-0377", "rule": "RAPID_MOVEMENT", "decision": "ESCALATE",
     "cps": ["Kestrel Partners SA"], "note": "Pass-through wires via Kestrel Partners SA; SAR filed."},
]


def seed_demo_history(mem: "AnalystMemory") -> list:
    """Idempotently add the demo analyst history. Returns the ids created (empty if already seeded)."""
    have = {t for m in mem.all() for t in m["tags"] if t.startswith("alert:")}
    created = []
    for c in SEED_CASES:
        if f"alert:{c['alert']}" in have:
            continue
        tags = ["origin:analyst", f"decision:{c['decision']}", f"cust:{c['cust']}", f"alert:{c['alert']}",
                f"rule:{c['rule']}"] + [f"cp:{norm_name(x)}" for x in c["cps"]]
        rec = mem.client.call(
            "add", content=(f"Analyst decided {c['decision']} on alert {c['alert']} ({c['rule']}) for customer "
                            f"{c['cust']}. Counterparties: {', '.join(c['cps'])}. {c['note']}"),
            source="analyst:seed-demo", confidence=0.9, tags=tags, layer="long_term")
        created.append(rec["id"])
    return created
