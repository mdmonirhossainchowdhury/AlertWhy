"""The investigation agent: Planner <-> Executor <-> Evaluator loop with shared memory and a full trace.

  Planner    chooses which checks to run (Meterless-served model if available, else a rule-based baseline)
  Executor   runs the checks as federated SQL through Zetaris and appends numbered evidence
  Evaluator  decides whether the evidence is sufficient; if not it names the MISSING checks and the loop re-plans
             (deterministic gates, plus an optional model critic that may only request existing checks)
  Memory     H-MEM recalls analyst-confirmed earlier cases before planning, remembers this one afterwards
  Narrator   writes the explanation; a citation guard rejects invented evidence ids

The verdict and score always come from the evidence engine, never from a model.
"""
import json
import threading
import time

from . import brain, config, llm, usage
from .federation import QueryLog, get_federation
from .hmem import AnalystMemory, MemoryUnavailable
from .tools import CHECKS, CONTEXT_CHECKS, PLANNABLE, RULE_PLAN, Ctx

NEXT_STEPS = {
    "ESCALATE": ["Escalate to the MLRO / compliance lead for suspicious-activity-report (SAR) review.",
                 "Freeze or restrict outbound activity per policy while review is open.",
                 "Request source-of-funds documentation from the customer."],
    "CLOSE": ["Close the alert as a false positive and record the cited mitigating evidence.",
              "Keep the customer on standard monitoring; no further action required."],
    "REVIEW": ["Route to a senior analyst: evidence is mixed or incomplete.",
               "Request clarification of the source of funds and purpose of the flagged transactions.",
               "Re-run after new information arrives."],
}


def decide(evidence):
    """Score, verdict and confidence from evidence only. Returns (score, verdict, confidence, conflict, strong)."""
    risk = sum(e["points"] for e in evidence if e["points"] > 0)
    mitig = sum(e["points"] for e in evidence if e["points"] < 0)
    score = max(0, min(100, risk + mitig))
    strong = any(e["detail"].get("strong") for e in evidence if e["tool"] == "sanctions")
    max_risk = max([e["points"] for e in evidence] or [0])
    if strong or score >= config.ESCALATE_AT:
        verdict = "ESCALATE"
    elif score <= config.CLOSE_AT:
        verdict = "CLOSE"
    else:
        verdict = "REVIEW"
    conflict = False
    if verdict == "CLOSE" and max_risk >= 25:  # a serious red flag outweighs the mitigations
        verdict, conflict = "REVIEW", True
    n_risk = len({e["tool"] for e in evidence if e["points"] > 0})
    n_mit = len({e["tool"] for e in evidence if e["points"] < 0})
    if verdict == "ESCALATE":
        conf = 0.60 + min(0.25, (score - config.ESCALATE_AT) / 160) + 0.05 * max(0, n_risk - 1)
        conf = max(conf, 0.85) if strong else conf
    elif verdict == "CLOSE":
        conf = 0.60 + min(0.2, (config.CLOSE_AT - score) / 100) + 0.05 * n_mit
    else:
        conf = 0.50
    return score, verdict, round(min(conf, 0.97), 2), conflict, strong


def evaluate(evidence, ran, verdict):
    """Deterministic sufficiency gates. Returns (missing_checks, reasons). Empty missing = evidence is sufficient."""
    ran, add, reasons = set(ran), [], []

    def want(names, why):
        n = [x for x in names if x not in ran and x not in add]
        if n:
            add.extend(n)
            reasons.append(f"{why}: {', '.join(n)}")

    risk_tools = {e["tool"] for e in evidence if e["points"] > 0 and e["tool"] != "memory"}
    if verdict in ("ESCALATE", "REVIEW") and any(e["points"] >= 20 for e in evidence) and len(risk_tools) == 1:
        want(["sanctions", "geography", "adverse_media", "novel_counterparty"],
             "a single-source red flag needs corroboration")
    if verdict == "CLOSE" and not ({"history", "recurring_batch"} & ran):
        want(["history", "recurring_batch"], "closing needs a checked innocent explanation")
    if any(e["tool"] == "sanctions" for e in evidence):
        want(["history", "geography"], "a watchlist match needs identity and behaviour context")
    if any(e["tool"] == "volume" and e["kind"] == "risk" for e in evidence):
        want(["pass_through", "geography"], "a volume spike needs flow and jurisdiction explanation")
    if verdict == "REVIEW":
        want(PLANNABLE, "inconclusive, widening the investigation")
    return add, reasons


class Investigator:
    def __init__(self, fed=None, router=None, memory="auto"):
        self.fed = fed or get_federation()
        self.router = router if router is not None else llm.build_router()
        if memory == "auto":
            memory = AnalystMemory() if config.HMEM_ENABLED else None
        self.memory = memory
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- helpers
    def list_alerts(self):
        return self.fed.query(
            "SELECT a.alert_id, a.customer_id, a.rule_id, a.rule_desc, a.triggered_at, k.name AS customer_name, "
            "k.kind, k.country FROM core_alerts a JOIN kyc k USING (customer_id) ORDER BY a.alert_id", [], QueryLog())

    def sponsor_status(self):
        z = self.fed.check()
        ai = self.router.status()
        mem = self.memory.status() if self.memory else {"state": "disabled", "records": 0, "error": None}
        return {
            "zetaris": {"live": self.fed.kind == "zetaris" and z["reachable"] and z["select1"], "backend": self.fed.backend,
                        "check": z},
            "meterless": {"providers": ai, "live": any(p["name"] == "meterless" and p["state"] == "online" for p in ai)},
            "hmem": mem,
        }

    # ---------------------------------------------------------------- the agent loop
    def investigate(self, alert_id: str, use_memory: bool = True) -> dict:
        t_start = time.perf_counter()
        log, meter, trace, limitations = QueryLog(), llm.TokenMeter(), [], []
        use_memory = bool(use_memory and self.memory)

        def step(rnd, role, action, detail, t0, **extra):
            trace.append({"round": rnd, "role": role, "action": action, "detail": detail,
                          "ms": round((time.perf_counter() - t0) * 1000, 1), **extra})

        t0 = time.perf_counter()
        rows = self.fed.query("SELECT * FROM core_alerts WHERE alert_id=?", [alert_id], log)
        if not rows:
            raise KeyError(alert_id)
        alert = rows[0]
        customer = self.fed.query("SELECT * FROM kyc WHERE customer_id=?", [alert["customer_id"]], log)[0]
        ctx = Ctx(self.fed, log, alert, customer)
        step(0, "executor", "load case", f"alert {alert_id} ({alert['rule_id']}), customer {customer['customer_id']}", t0)

        evidence, audit, ran, plan_history = [], [], [], []

        def add_evidence(items):
            for e in items:
                e["id"] = f"E{len(evidence) + 1}"
                evidence.append(e)

        def run_checks(names, rnd):
            for name in names:
                if name in ran:
                    continue
                chk, before, t1 = CHECKS[name], len(log.records), time.perf_counter()
                try:
                    items = chk.fn(ctx)
                    err = None
                except Exception as e:  # a failing source must not kill the case; surface it as a limitation
                    items, err = [], f"{type(e).__name__}: {e}"
                    limitations.append(f"Check '{name}' failed and was skipped: {err[:160]}")
                add_evidence(items)
                ran.append(name)
                audit.append({"step": chk.label, "check": name, "round": rnd, "error": err, "findings": len(items),
                              "queries": [{"sql": r.sql, "params": [str(p) for p in r.params], "rows": r.rows,
                                           "ms": round(r.ms, 2)} for r in log.records[before:]]})
                step(rnd, "executor", f"run {name}", f"{len(items)} finding(s), {len(log.records) - before} query(ies)", t1)

        # ---- round 0: context + memory recall
        run_checks(CONTEXT_CHECKS, 0)
        memory_state = {"enabled": use_memory, "recalled": [], "remembered_id": None, "error": None}
        counterparties = []
        try:
            counterparties = [c["cp_name"] for c in ctx.counterparties()]
        except Exception as e:  # noqa: BLE001
            limitations.append(f"Counterparty lookup failed: {str(e)[:120]}")
        if use_memory:
            t1 = time.perf_counter()
            try:
                recalled = self.memory.recall(alert, customer, counterparties)
                add_evidence(recalled)
                memory_state["recalled"] = [e["detail"].get("memory_id") for e in recalled]
                step(0, "memory", "recall (H-MEM)", f"{len(recalled)} relevant earlier case(s)", t1)
            except MemoryUnavailable as e:
                memory_state["error"] = str(e)
                limitations.append(f"Memory unavailable: {str(e)[:160]}")

        # ---- rounds
        planner = "rules"
        pending, explored = [], 0
        for rnd in range(1, config.MAX_ROUNDS + 1):
            t1 = time.perf_counter()
            if rnd == 1:
                baseline = RULE_PLAN.get(alert["rule_id"], PLANNABLE)
                memory_lines = [e["finding"] for e in evidence if e["tool"] == "memory"]
                plan = None
                if self.router.enabled:
                    plan = brain.plan(self.router, meter, alert, customer, memory_lines, baseline)
                if plan:
                    ai_names, why = plan
                    # SAFETY FLOOR: the AI may ADD checks, never remove the ones the triggering rule requires.
                    # (A planner that skips the rule's core check could silently close a true positive.)
                    added = [n for n in ai_names if n not in baseline]
                    names = list(baseline) + added
                    why = (f"rule baseline [{', '.join(baseline)}] + AI added [{', '.join(added) or 'nothing'}]. "
                           f"AI rationale: {why}")
                    planner = "llm"
                    dropped = [n for n in baseline if n not in ai_names]
                    if dropped:
                        limitations.append("The AI planner left out required checks for this rule (" + ", ".join(dropped)
                                           + "); they were run anyway.")
                else:
                    names, why = baseline, f"rule-based plan for {alert['rule_id']}"
                    if self.router.enabled:
                        limitations.append("The AI planner was unavailable or returned an invalid plan; the "
                                           "rule-based plan was used.")
            else:
                names, why = pending, "evaluator found missing evidence"
            plan_history.append({"round": rnd, "checks": names, "why": why, "planner": planner if rnd == 1 else "evaluator"})
            step(rnd, "planner", f"plan: {', '.join(names)}", why, t1)

            run_checks(names, rnd)

            if rnd == 1 and self.router.enabled and config.EXPLORE_MAX_QUERIES > 0:
                t1 = time.perf_counter()
                recs = brain.explore(self.router, meter, self.fed, log, alert, customer, config.EXPLORE_MAX_QUERIES)
                for r in recs:
                    if r["ok"]:
                        explored += 1
                        first = json.dumps(r["preview"][0], default=str)[:160] if r["preview"] else "no rows"
                        add_evidence([{"tool": "explore", "source": "federated-sql (AI-written, read-only)",
                                       "kind": "info", "points": 0,
                                       "finding": f"Exploratory query returned {r['rows']} row(s). First row: {first}",
                                       "detail": {"sql": r["sql"], "rows": r["rows"], "preview": r["preview"]}}])
                step(rnd, "executor", "AI exploration (guarded SQL)", f"{explored} query(ies) accepted of {len(recs)}", t1)

            score, verdict, conf, _, _ = decide(evidence)
            missing, reasons = evaluate(evidence, ran, verdict)
            t1 = time.perf_counter()
            critic_note = ""
            if self.router.enabled:
                avail = [n for n in PLANNABLE if n not in ran and n not in missing]
                crit = brain.critique(self.router, meter, alert, evidence, verdict, score, ran, avail)
                if crit:
                    critic_note = crit["reason"]
                    for n in crit["additional"]:
                        if n not in missing:
                            missing.append(n)
                            reasons.append(f"AI critic requested: {n}")
            pending = missing
            step(rnd, "evaluator", "needs more evidence" if pending else "evidence sufficient",
                 "; ".join(reasons) or critic_note or f"provisional {verdict} at {score}/100", t1,
                 provisional={"verdict": verdict, "score": score})
            if not pending:
                break

        # ---- final decision
        score, verdict, conf, conflict, strong = decide(evidence)
        rounds = len(plan_history)
        if pending:
            limitations.append(f"Evidence gaps remained after {rounds} round(s): {', '.join(pending)}.")
            if verdict == "CLOSE":
                verdict, conf = "REVIEW", 0.5
                limitations.append("Auto-closure was blocked because requested evidence could not be gathered.")
        limitations += ["Decision support only: a human analyst must confirm before any filing or account action.",
                        "Based only on the sources reachable through the federation layer; no external enrichment."]
        if conflict:
            limitations.append("Mitigating evidence exists but a serious red flag prevents auto-closure.")

        # ---- narration
        t1 = time.perf_counter()
        narrative, source = self._template(alert, verdict, score, conf, evidence), "template"
        budget_exceeded = False
        if self.router.enabled:
            spent = usage.cost_usd(log.count, meter.tokens_in + meter.tokens_out)
            if spent + usage.cost_usd(0, 900) > config.MAX_COST_PER_INVESTIGATION_USD:
                budget_exceeded = True
                limitations.append("AI narration skipped: the per-investigation cost budget would be exceeded.")
            else:
                text = brain.narrate(self.router, meter, alert, verdict, score, conf, evidence)
                if text:
                    narrative, source = text, "ai:" + (sorted(meter.models)[-1] if meter.models else "provider")
                else:
                    limitations.append("AI narration unavailable or failed the citation guard; template used.")
        step(rounds, "narrator", f"explain ({source.split(':')[0]})", f"{len(narrative)} characters", t1)

        # ---- remember (agent-origin, never scored later)
        entities = sorted({e["detail"]["entity"] for e in evidence if e["tool"] == "sanctions" and "entity" in e["detail"]})
        if use_memory and not memory_state["error"]:
            try:
                rec = self.memory.remember_investigation(alert, customer, verdict, score, conf, counterparties, entities)
                memory_state["remembered_id"] = rec["id"]
            except MemoryUnavailable as e:
                memory_state["error"] = str(e)

        duration = (time.perf_counter() - t_start) * 1000
        event = usage.record(alert_id, log.count, log.rows, meter, duration, verdict, rounds, budget_exceeded)
        manual = usage.manual_cost_usd()
        iso = lambda d: {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in d.items()}  # noqa: E731
        result = {
            "alert": iso(alert), "customer": iso(customer),
            "plan": [CHECKS[n].label for n in ran if CHECKS[n].category != "context"], "plan_history": plan_history,
            "rounds": rounds, "planner": planner, "evidence": evidence, "risk_score": score, "verdict": verdict,
            "confidence": conf, "narrative": narrative, "narrative_source": source,
            "next_steps": NEXT_STEPS[verdict], "limitations": limitations, "trace": trace,
            "context": {"counterparties": counterparties, "entities": entities},
            "memory": memory_state,
            "ai": {"enabled": self.router.enabled, "calls": meter.calls, "tokens_in": meter.tokens_in,
                   "tokens_out": meter.tokens_out, "models": sorted(meter.models), "providers": sorted(meter.providers)},
            "usage": {"queries": log.count, "rows_returned": log.rows, "duration_ms": round(duration, 1),
                      "cost_usd": event["data"]["cost_usd"], "manual_cost_usd": manual,
                      "saved_usd": round(manual - event["data"]["cost_usd"], 2), "event_id": event["id"],
                      "budget_exceeded": budget_exceeded},
            "federation_backend": self.fed.backend, "audit": audit,
        }
        out_dir = config.DATA_DIR / "investigations"
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / f"{alert_id}.json", "w") as f:
            json.dump(result, f, indent=2, default=str)
        return result

    # ---------------------------------------------------------------- analyst feedback -> memory
    def analyst_feedback(self, alert_id: str, decision: str, note: str = "") -> dict:
        if not self.memory:
            raise MemoryUnavailable("memory is disabled (HMEM_ENABLED=false)")
        if decision not in ("ESCALATE", "CLOSE", "REVIEW"):
            raise ValueError("decision must be ESCALATE, CLOSE or REVIEW")
        path = config.DATA_DIR / "investigations" / f"{alert_id}.json"
        if not path.exists():
            raise KeyError(f"investigate {alert_id} first")
        r = json.load(open(path))
        used = [e["detail"]["memory_id"] for e in r["evidence"]
                if e["tool"] == "memory" and e["points"] != 0 and e["detail"].get("memory_id")]
        rec = self.memory.record_analyst_decision(
            r["alert"], r["customer"], decision, r["verdict"], r["context"]["counterparties"],
            r["context"]["entities"], note, r["memory"].get("remembered_id"), used)
        return {"saved": rec, "agrees_with_agent": decision == r["verdict"]}

    @staticmethod
    def _template(alert, verdict, score, conf, evidence):
        risks = sorted([e for e in evidence if e["points"] > 0], key=lambda e: -e["points"])
        mits = sorted([e for e in evidence if e["points"] < 0], key=lambda e: e["points"])
        head = {"ESCALATE": "Recommend ESCALATION", "CLOSE": "Recommend CLOSING as a likely false positive",
                "REVIEW": "Recommend HUMAN REVIEW"}[verdict]
        s = f"{head} of alert {alert['alert_id']} ({alert['rule_desc'].lower()}): risk {score}/100, confidence {conf:.0%}. "
        if risks:
            s += "Why: " + " ".join(f"{e['finding']} [{e['id']}]" for e in risks[:4]) + " "
        else:
            s += "No independent red flags were found beyond the triggering rule. "
        if mits:
            s += "Mitigating: " + " ".join(f"{e['finding']} [{e['id']}]" for e in mits[:2])
        return s.strip()
