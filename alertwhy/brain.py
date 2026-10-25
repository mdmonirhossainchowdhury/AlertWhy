"""The model-driven parts of the agent: planner, critic, SQL explorer, narrator.

Design rule: the model PROPOSES, the evidence engine DISPOSES. Everything a model returns is validated:
  * planner / critic may only name checks that exist in the catalog (unknown names are dropped);
  * the explorer may only run guarded read-only SQL; its results become 0-point `info` evidence built from the
    real returned rows, never from the model's prose;
  * the narrator may only cite evidence ids that exist (citation guard), else the deterministic template is used.
No model output can change a score or a verdict.
"""
import json
import re

from . import sqlguard
from .federation import FederationError, TABLE_COLUMNS
from .llm import ProviderError
from .tools import CHECKS, PLANNABLE

SYSTEM = ("You are an AML investigation assistant working for a bank compliance team. Be precise, never invent facts, "
          "and prefer saying 'unknown' to guessing.")


def clean(text: str) -> str:
    text = re.sub(r"(?s)<think>.*?</think>", "", text or "")
    return text.strip()


def parse_json(text: str):
    t = clean(text)
    t = re.sub(r"^```(?:json)?|```$", "", t.strip(), flags=re.M).strip()
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                return None
    return None


def catalog_text(names):
    return "\n".join(f"- {n}: {CHECKS[n].label}" for n in names)


def plan(router, meter, alert, customer, memory_lines, baseline):
    """Round-1 plan. Returns (checks, rationale) or None when the model is unavailable/invalid."""
    prompt = (
        f"Alert {alert['alert_id']}: rule {alert['rule_id']} - {alert['rule_desc']}.\n"
        f"Customer: {customer['name']} ({customer['kind']}, {customer['country']}), expected monthly volume "
        f"${customer['expected_monthly_usd']:,.0f}.\n"
        + ("Relevant memory from earlier cases:\n" + "\n".join(memory_lines) + "\n" if memory_lines else "")
        + "Choose 3-5 checks to run FIRST from this catalog (you will get more rounds if evidence is missing):\n"
        + catalog_text(PLANNABLE)
        + '\nReply with JSON only: {"checks": ["name", ...], "rationale": "one sentence"}'
    )
    try:
        resp = router.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                           max_tokens=300)
    except ProviderError:
        return None
    meter.add(resp)
    data = parse_json(resp["message"].get("content", ""))
    if not isinstance(data, dict) or not isinstance(data.get("checks"), list):
        return None
    names = [n for n in data["checks"] if isinstance(n, str) and n in PLANNABLE]
    names = list(dict.fromkeys(names))
    if not names:
        return None
    return names, str(data.get("rationale", ""))[:300]


def critique(router, meter, alert, evidence, verdict, score, ran, available):
    """Ask the model whether evidence is sufficient. Returns {"approve", "additional", "reason"} or None."""
    lines = "\n".join(f"[{e['id']}] ({e['kind']}, {e['points']:+d}) {e['finding']}" for e in evidence)
    prompt = (
        f"Alert {alert['alert_id']} ({alert['rule_id']}). Provisional decision from the rules engine: {verdict} "
        f"(risk {score}/100).\nEvidence so far:\n{lines}\nChecks already run: {', '.join(ran)}.\n"
        f"Checks still available:\n{catalog_text(available) if available else '(none)'}\n"
        "Act as a skeptical reviewer. Is there missing evidence that could contradict or confirm the decision? "
        'Reply with JSON only: {"approve": true|false, "additional_checks": ["name", ...], "reason": "one sentence"}'
    )
    try:
        resp = router.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                           max_tokens=300)
    except ProviderError:
        return None
    meter.add(resp)
    data = parse_json(resp["message"].get("content", ""))
    if not isinstance(data, dict):
        return None
    add = [n for n in (data.get("additional_checks") or []) if isinstance(n, str) and n in available]
    return {"approve": bool(data.get("approve", not add)) and not add, "additional": list(dict.fromkeys(add)),
            "reason": str(data.get("reason", ""))[:300]}


SQL_TOOL = [{
    "type": "function",
    "function": {
        "name": "run_sql",
        "description": "Run ONE read-only SELECT over the federated bank data. Tables and columns: "
                       + "; ".join(f"{t}({', '.join(c)})" for t, c in TABLE_COLUMNS.items())
                       + ". Spark SQL dialect. Max 200 rows. Give EVERY selected expression an alias (AS name); "
                       "SELECT * is only allowed on a single table.",
        "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
    },
}]


def explore(router, meter, fed, log, alert, customer, max_queries):
    """Let the model run a few guarded read-only queries of its own. Returns a list of exploration records:
    {sql, ok, rows, preview|error}. The caller turns them into info evidence."""
    msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": (
            f"Alert {alert['alert_id']} ({alert['rule_id']}) on customer {customer['customer_id']} "
            f"({customer['name']}). Use run_sql (at most {max_queries} calls) to look for anything unusual that "
            "the standard checks might miss, for example other customers paying the same counterparties, or "
            "repeated amounts. Then stop and reply with one short sentence.")},
    ]
    records = []
    for _ in range(max_queries + 1):
        try:
            resp = router.chat(msgs, tools=SQL_TOOL, max_tokens=500)
        except ProviderError:
            break
        meter.add(resp)
        msg = resp["message"]
        calls = msg.get("tool_calls") or []
        if not calls or len(records) >= max_queries:
            break
        msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            if len(records) >= max_queries:
                msgs.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": "query budget exhausted"})
                continue
            args = {}
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                safe = sqlguard.validate(args.get("sql", ""))
                rows = fed.query(safe, [], log)
                preview = [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in rows[:5]]
                records.append({"sql": safe, "ok": True, "rows": len(rows), "preview": preview})
                result = json.dumps({"rows": len(rows), "first_rows": preview}, default=str)[:3000]
            except (sqlguard.SQLGuardError, FederationError, ValueError, KeyError) as e:
                records.append({"sql": str(args.get("sql", ""))[:300], "ok": False, "rows": 0,
                                "error": str(e)[:200]})
                result = f"error: {e}"
            msgs.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
    return records


def guard_ok(text: str, evidence) -> bool:
    cited = set(re.findall(r"\[(E\d+)\]", text))
    valid = {e["id"] for e in evidence}
    return bool(cited) and cited <= valid


def narrate(router, meter, alert, verdict, score, confidence, evidence):
    """Return narrative text, or None (unavailable, or failed the citation guard)."""
    lines = "\n".join(f"[{e['id']}] ({e['kind']}, {e['points']:+d}) {e['finding']}"
                      for e in evidence if e["points"] or e["kind"] == "info")
    prompt = (
        f"Write a concise 3-5 sentence case rationale for a compliance officer.\nAlert {alert['alert_id']} rule "
        f"{alert['rule_id']}. Decision already made by the rules engine: {verdict} (risk {score}/100, confidence "
        f"{confidence}).\nUse ONLY the evidence below. Cite evidence IDs like [E2] after each claim. Do not invent "
        f"facts, names or amounts. Mention the strongest mitigating evidence if any.\n\nEVIDENCE:\n{lines}"
    )
    try:
        resp = router.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                           max_tokens=450)
    except ProviderError:
        return None
    meter.add(resp)
    text = clean(resp["message"].get("content", ""))
    return text if guard_ok(text, evidence) else None
