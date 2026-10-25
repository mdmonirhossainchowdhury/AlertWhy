"""CLI:  python -m alertwhy.cli  generate | export-pg | list | investigate A-1001 [--no-memory] | eval | status | seed-memory"""
import csv
import sys

from . import config, datagen, usage
from .agent import Investigator


def evaluate(inv=None):
    """Run every alert (memory OFF, so this measures the evidence engine only) and compare with planted labels."""
    inv = inv or Investigator()
    truth = {r["alert_id"]: r for r in csv.DictReader(open(config.DATA_DIR / "eval" / "ground_truth.csv"))}
    rows, correct = [], 0
    for a in inv.list_alerts():
        r = inv.investigate(a["alert_id"], use_memory=False)
        t = truth[a["alert_id"]]
        ok = r["verdict"] == t["label"]
        correct += ok
        rows.append({"alert_id": a["alert_id"], "rule": a["rule_id"], "expected": t["label"], "got": r["verdict"],
                     "score": r["risk_score"], "confidence": r["confidence"], "ok": ok, "rounds": r["rounds"],
                     "cost_usd": r["usage"]["cost_usd"], "why": t["why"]})
    n = len(rows)
    esc = [r for r in rows if r["expected"] == "ESCALATE"]
    clo = [r for r in rows if r["expected"] == "CLOSE"]
    return {
        "total": n, "correct": correct, "accuracy": round(correct / n, 3),
        "escalate_recall": round(sum(r["ok"] for r in esc) / len(esc), 3) if esc else None,
        "close_precision": round(sum(r["ok"] for r in clo) / len(clo), 3) if clo else None,
        "missed_escalations": [r["alert_id"] for r in esc if not r["ok"]],
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 6), "rows": rows,
    }


def _line(ok, name, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" - {detail}" if detail else ""))
    return ok


def status():
    inv = Investigator()
    s = inv.sponsor_status()
    z = s["zetaris"]
    zetaris_ok = z["live"] and z["check"]["reachable"] and z["check"]["select1"]
    ok = _line(zetaris_ok, "Data: Zetaris REST SELECT 1",
               z["backend"] + (f" | {z['check']['error']}" if z["check"]["error"] else ""))
    for t, n in z["check"]["tables"].items():
        ok &= _line(isinstance(n, int), f"Data: table {t}", str(n))
    if z["backend"].startswith("local"):
        print("NOTE the data layer is the OFFLINE FALLBACK, not Zetaris. Set ZETARIS_REST_URL and ZETARIS_API_KEY.")
    if not s["meterless"]["providers"]:
        ok &= _line(False, "Power: Meterless Nightshift",
                    "not configured (set METERLESS_BASE_URL and METERLESS_RUNTIME_TOKEN)")
    for p in s["meterless"]["providers"]:
        ok &= _line(p["state"] == "online", f"Power: {p['name']} models endpoint",
                    p["error"] or f"{p['base_url']} model={p['model']}")
    m = s["hmem"]
    ok &= _line(m["state"] == "online", "Build: Meterless H-MEM engine",
                m["error"] or f"{m['records']} records")
    return ok


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "help"
    if cmd != "generate" and not (config.DATA_DIR / "core_banking.duckdb").exists():
        print("First run: generating the synthetic bank data (about 1 second)...")
        datagen.build()
    if cmd == "generate":
        print("Synthetic data written to", datagen.build())
    elif cmd == "export-pg":
        from . import pgexport
        print(pgexport.export(config.DATA_DIR))
    elif cmd == "list":
        for a in Investigator().list_alerts():
            print(f"{a['alert_id']}  {a['rule_id']:<22} {a['customer_name']}")
    elif cmd == "investigate" and len(argv) > 2:
        r = Investigator().investigate(argv[2], use_memory="--no-memory" not in argv)
        print(f"{r['alert']['alert_id']}: {r['verdict']}  risk={r['risk_score']}  confidence={r['confidence']}  rounds={r['rounds']}")
        print(r["narrative"])
        u = r["usage"]
        print(f"[{u['queries']} queries, {r['ai']['calls']} AI calls, ${u['cost_usd']} vs ${u['manual_cost_usd']} manual | {r['federation_backend']}]")
    elif cmd == "eval":
        usage.reset()
        res = evaluate()
        for r in res["rows"]:
            print(f"{'OK  ' if r['ok'] else 'MISS'} {r['alert_id']} expected={r['expected']:<8} got={r['got']:<8} "
                  f"score={r['score']:>3} conf={r['confidence']} rounds={r['rounds']}  {r['why']}")
        print(f"\naccuracy {res['correct']}/{res['total']} = {res['accuracy']:.0%} | escalation recall "
              f"{res['escalate_recall']} | total cost ${res['total_cost_usd']}")
    elif cmd == "status":
        sys.exit(0 if status() else 1)
    elif cmd == "seed-memory":
        from .hmem import seed_demo_history
        inv = Investigator()
        print("created:", seed_demo_history(inv.memory) if inv.memory else "memory disabled")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
