"""Investigation checks. Each check runs federated SQL (logical table names) and returns evidence items.

Evidence item: {tool, source, kind: risk|mitigating|info, points, finding, detail}
Positive points raise risk, negative points are mitigating. The agent (agent.py) chooses WHICH checks to run,
in rounds; a check never decides the verdict by itself.
"""
import re
from collections import namedtuple
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from . import config

SUFFIXES = {"inc", "ltd", "llc", "co", "group", "sa", "gmbh", "corp", "fze"}


def _as_of():
    return datetime.fromisoformat(config.AS_OF + "T23:59:59")


def norm_name(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return " ".join(t for t in s.split() if t not in SUFFIXES)


def name_sim(a: str, b: str) -> float:
    """Similarity tolerant of reordering and middle names/initials (first+last token equal => 0.92)."""
    ta, tb = norm_name(a).split(), norm_name(b).split()
    if not ta or not tb:
        return 0.0
    score = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    if len(ta) > 1 and len(tb) > 1 and ta[0] == tb[0] and ta[-1] == tb[-1]:
        score = max(score, 0.92)
    return round(score, 3)


def ev(tool, source, kind, points, finding, **detail):
    return {"tool": tool, "source": source, "kind": kind, "points": points, "finding": finding, "detail": detail}


class Ctx:
    def __init__(self, fed, log, alert, customer):
        self.fed, self.log, self.alert, self.customer = fed, log, alert, customer
        self.cid = alert["customer_id"]
        self.asof = _as_of()
        self.since30 = self.asof - timedelta(days=30)
        self._tier = None
        self._trig = None
        self._cps = None

    def q(self, sql, params=None):
        return self.fed.query(sql, params, self.log)

    def trigger_txns(self):
        if self._trig is None:
            ids = [i for i in (self.alert["trigger_txn_ids"] or "").split(",") if i]
            if not ids:
                self._trig = []
            else:
                ph = ",".join("?" * len(ids))
                self._trig = self.q(f"SELECT * FROM core_transactions WHERE txn_id IN ({ph}) ORDER BY ts", ids)
        return self._trig

    def counterparties(self):
        """30-day counterparties of the customer: [{cp_name, cp_country, total_usd}] (one cached query)."""
        if self._cps is None:
            self._cps = self.q("SELECT counterparty_name AS cp_name, counterparty_country AS cp_country, "
                               "SUM(amount_usd) AS total_usd FROM core_transactions WHERE customer_id=? AND ts> ? "
                               "AND ts<= ? GROUP BY counterparty_name, counterparty_country",
                               [self.cid, self.since30, self.asof])
        return self._cps

    def tier(self):
        if self._tier is None:
            self._tier = {r["country"]: r["risk_tier"] for r in self.q("SELECT country, risk_tier FROM jurisdictions")}
        return self._tier


def tenure_days(ctx):
    return (ctx.asof.date() - ctx.customer["onboarded"]).days


# ------------------------------------------------------------------ checks
def profile(ctx):
    c = ctx.customer
    return [ev("profile", "kyc", "info", 0,
               f"{c['name']} is a {c['kind']} ({c['occupation']}), country {c['country']}, onboarded "
               f"{tenure_days(ctx)} days ago, expected monthly volume ${c['expected_monthly_usd']:,.0f}.",
               tenure_days=tenure_days(ctx))]


def volume(ctx):
    c = ctx.customer
    r = ctx.q("SELECT SUM(amount_usd) AS total_usd, COUNT(*) AS cnt FROM core_transactions "
              "WHERE customer_id=? AND ts> ? AND ts<= ?", [ctx.cid, ctx.since30, ctx.asof])[0]
    total = r["total_usd"] or 0
    exp = max(c["expected_monthly_usd"], 1)
    ratio = total / exp
    if ratio >= 5:
        return [ev("volume", "core_banking+kyc", "risk", 20,
                   f"30-day activity ${total:,.0f} is {ratio:.1f}x the expected monthly volume.", ratio=round(ratio, 2))]
    if ratio >= 3:
        return [ev("volume", "core_banking+kyc", "risk", 12,
                   f"30-day activity ${total:,.0f} is {ratio:.1f}x the expected monthly volume.", ratio=round(ratio, 2))]
    if ratio <= 1.5:
        return [ev("volume", "core_banking+kyc", "mitigating", -8,
                   f"30-day activity ${total:,.0f} is in line with expected volume ({ratio:.1f}x).", ratio=round(ratio, 2))]
    return []


def structuring(ctx):
    rows = ctx.q("SELECT ts, amount_usd FROM core_transactions WHERE customer_id=? AND channel='cash' AND "
                 "direction='IN' AND amount_usd BETWEEN 8000 AND 9999.99 AND ts> ? AND ts<= ? ORDER BY ts",
                 [ctx.cid, ctx.since30, ctx.asof])
    best = []
    for i, r in enumerate(rows):
        window = [x for x in rows[i:] if x["ts"] - r["ts"] <= timedelta(days=14)]
        if len(window) > len(best):
            best = window
    if len(best) >= 3:
        total = sum(x["amount_usd"] for x in best)
        return [ev("structuring", "core_banking", "risk", min(40, 10 + 4 * len(best)),
                   f"{len(best)} cash deposits of $8,000-$9,999 within 14 days (total ${total:,.0f}), "
                   f"a pattern consistent with structuring below a $10,000 reporting threshold.",
                   count=len(best), total=round(total, 2))]
    return []


def pass_through(ctx):
    rows = ctx.q("SELECT direction, SUM(amount_usd) AS total_usd FROM core_transactions WHERE customer_id=? AND "
                 "channel='wire' AND ts> ? AND ts<= ? GROUP BY direction", [ctx.cid, ctx.since30, ctx.asof])
    d = {r["direction"]: r["total_usd"] for r in rows}
    inn, out = d.get("IN", 0) or 0, d.get("OUT", 0) or 0
    if inn >= 50000 and 0.85 <= out / inn <= 1.15:
        pts = 25 + (10 if tenure_days(ctx) < 180 else 0)
        return [ev("pass_through", "core_banking", "risk", pts,
                   f"Wires in ${inn:,.0f} vs wires out ${out:,.0f} in 30 days ({out / inn:.0%} passed straight through)"
                   + (f" on an account only {tenure_days(ctx)} days old." if tenure_days(ctx) < 180 else "."),
                   inflow=round(inn, 2), outflow=round(out, 2))]
    return []


def geography(ctx):
    tier = ctx.tier()
    rows = ctx.q("SELECT counterparty_country AS cp_country, SUM(amount_usd) AS total_usd FROM core_transactions "
                 "WHERE customer_id=? AND ts> ? AND ts<= ? GROUP BY counterparty_country",
                 [ctx.cid, ctx.since30, ctx.asof])
    total = sum((r["total_usd"] or 0) for r in rows) or 1
    share = {}
    for r in rows:
        t = tier.get(r["cp_country"], "unknown")
        share[t] = share.get(t, 0) + (r["total_usd"] or 0) / total
    hi, el, me = share.get("high", 0), share.get("elevated", 0), share.get("medium", 0)
    countries = sorted({r["cp_country"] for r in rows if tier.get(r["cp_country"]) in ("high", "elevated")})
    if hi >= 0.10:
        return [ev("geography", "core_banking+jurisdictions", "risk", 30,
                   f"{hi:.0%} of 30-day value moves to/from high-risk jurisdictions ({', '.join(countries)}).",
                   share=round(hi, 3))]
    if el >= 0.30:
        return [ev("geography", "core_banking+jurisdictions", "risk", 20,
                   f"{el:.0%} of 30-day value moves to/from elevated-risk jurisdictions ({', '.join(countries)}).",
                   share=round(el, 3))]
    if el >= 0.10:
        return [ev("geography", "core_banking+jurisdictions", "risk", 10,
                   f"{el:.0%} of 30-day value touches elevated-risk jurisdictions ({', '.join(countries)}).",
                   share=round(el, 3))]
    if me >= 0.30:
        return [ev("geography", "core_banking+jurisdictions", "risk", 5,
                   f"{me:.0%} of 30-day value touches medium-risk jurisdictions.", share=round(me, 3))]
    return []


def sanctions(ctx):
    out = []
    c = ctx.customer
    watch = ctx.q("SELECT * FROM sanctions")
    best = None
    for w in watch:
        names = [w["name"]] + [a for a in (w["aliases"] or "").split(";") if a.strip()]
        sim = max(name_sim(c["name"], n) for n in names)
        if sim >= 0.85 and (best is None or sim > best[0]):
            best = (sim, w)
    if best:
        sim, w = best
        w_dob = str(w["dob"]) if w["dob"] not in (None, "") else ""  # CSV/REST sources may type it as DATE
        dob_eq = bool(w_dob) and w_dob == str(c["dob"])
        ctry_eq = w["country"] == c["country"]
        if w_dob:
            if dob_eq and ctry_eq:
                out.append(ev("sanctions", "sanctions_list+kyc", "risk", 60,
                              f"Customer name matches watchlist entry {w['entity_id']} ({w['name']}, {sim:.0%}) and BOTH "
                              f"date of birth ({w_dob}) and country ({w['country']}) match.",
                              entity=w["entity_id"], strong=True))
            elif dob_eq or ctry_eq:
                out.append(ev("sanctions", "sanctions_list+kyc", "risk", 25,
                              f"Customer name matches watchlist entry {w['entity_id']} ({sim:.0%}); only one of DOB/country matches.",
                              entity=w["entity_id"], strong=False))
            else:
                out.append(ev("sanctions", "sanctions_list+kyc", "mitigating", -10,
                              f"Name resembles watchlist entry {w['entity_id']} ({sim:.0%}) but date of birth "
                              f"({c['dob']} vs {w_dob}) and country ({c['country']} vs {w['country']}) both differ - "
                              f"likely a different person.", entity=w["entity_id"]))
        elif ctry_eq:
            out.append(ev("sanctions", "sanctions_list+kyc", "risk", 40,
                          f"Entity name matches watchlist entry {w['entity_id']} ({sim:.0%}) and country matches.",
                          entity=w["entity_id"], strong=True))
    for cp in ctx.counterparties():
        for w in watch:
            names = [w["name"]] + [a for a in (w["aliases"] or "").split(";") if a.strip()]
            sim = max(name_sim(cp["cp_name"], n) for n in names)
            if sim >= 0.9 and cp["cp_country"] == w["country"]:
                out.append(ev("sanctions", "sanctions_list+core_banking", "risk", 45,
                              f"Counterparty '{cp['cp_name']}' ({cp['cp_country']}) matches watchlist entry {w['entity_id']} "
                              f"({sim:.0%}); ${cp['total_usd']:,.0f} moved in 30 days.", entity=w["entity_id"],
                              strong=True, counterparty=cp["cp_name"]))
    return out


def adverse_media(ctx):
    c, out = ctx.customer, []
    for m in ctx.q("SELECT * FROM media"):
        if name_sim(c["name"], m["entity"]) >= 0.95 and m["country"] == c["country"]:
            age = (ctx.asof.date() - datetime.fromisoformat(str(m["date"])[:10]).date()).days
            pts = {"high": 25, "medium": 12}.get(m["severity"], 4)
            if age > 365 * 3:
                pts = max(0, pts // 3)
            out.append(ev("adverse_media", "adverse_media", "risk", pts,
                          f"Adverse media {age} days ago ({m['category']}, severity {m['severity']}): \"{m['headline']}\" "
                          f"- {m['source']}.", category=m["category"], severity=m["severity"]))
    return out


def novel_counterparty(ctx):
    tier = ctx.tier()
    flagged = []
    for t in ctx.trigger_txns():
        if tier.get(t["counterparty_country"]) in ("elevated", "high"):
            prior = ctx.q("SELECT COUNT(*) AS cnt FROM core_transactions WHERE customer_id=? AND counterparty_name=? "
                          "AND ts< ?", [ctx.cid, t["counterparty_name"], t["ts"]])[0]["cnt"]
            if prior == 0:
                flagged.append(f"{t['counterparty_name']} ({t['counterparty_country']}, ${t['amount_usd']:,.0f})")
    if flagged:
        return [ev("novel_counterparty", "core_banking+jurisdictions", "risk", 15,
                   "First-ever dealings with elevated/high-risk jurisdiction counterparties in the alerted "
                   "transactions: " + "; ".join(flagged[:3]) + ".", count=len(flagged))]
    return []


def dormancy(ctx):
    rows = ctx.q("SELECT ts FROM core_transactions WHERE customer_id=? AND ts<= ? ORDER BY ts", [ctx.cid, ctx.asof])
    ts = [r["ts"] for r in rows]
    for i in range(1, len(ts)):
        gap = (ts[i] - ts[i - 1]).days
        burst = [t for t in ts[i:] if t - ts[i] <= timedelta(days=30)]
        if gap >= 180 and len(burst) >= 3 and (ctx.asof - ts[i]).days <= 60:
            return [ev("dormancy", "core_banking", "risk", 20,
                       f"Account was dormant for {gap} days, then {len(burst)} transactions within 30 days of reactivation.",
                       gap_days=gap, burst=len(burst))]
    return []


def history_consistency(ctx):
    """Mitigating: the alerted activity matches this customer's own long-standing behaviour."""
    if tenure_days(ctx) < 365:
        return []
    trig = ctx.trigger_txns()
    if not trig:
        return []
    pct = ctx.fed.dialect.pct("amount_usd", 0.9)
    ok = 0
    for t in trig:
        cutoff = t["ts"] - timedelta(days=60)
        h = ctx.q(f"SELECT COUNT(*) AS cnt, {pct} AS p90_usd FROM core_transactions "
                  "WHERE customer_id=? AND counterparty_name=? AND direction=? AND ts< ?",
                  [ctx.cid, t["counterparty_name"], t["direction"], cutoff])[0]
        if h["cnt"] >= 3 and h["p90_usd"] and t["amount_usd"] <= 3 * h["p90_usd"]:
            ok += 1
    if ok == len(trig):
        return [ev("history", "core_banking", "mitigating", -20,
                   f"All {len(trig)} alerted transaction(s) match long-standing behaviour: same counterparty seen "
                   f"repeatedly over months, with amounts within normal range.", checked=len(trig))]
    return []


def recurring_batch(ctx):
    """Mitigating: many same-day outgoing ACH payments that recur (payroll-like)."""
    if tenure_days(ctx) < 365:
        return []
    rows = ctx.q("SELECT CAST(ts AS DATE) AS day, COUNT(*) AS cnt FROM core_transactions WHERE customer_id=? AND "
                 "direction='OUT' AND channel='ach' AND ts<= ? GROUP BY CAST(ts AS DATE) HAVING COUNT(*)>=10",
                 [ctx.cid, ctx.asof])
    if len(rows) >= 3:
        return [ev("recurring_batch", "core_banking", "mitigating", -15,
                   f"{len(rows)} prior days with 10+ same-day outgoing ACH payments: a recurring payroll-style batch "
                   f"rather than anomalous velocity.", batch_days=len(rows))]
    return []


Check = namedtuple("Check", "name label fn category")
# category: context (always run first) | risk (looks for red flags) | mitigation (looks for innocent explanations)
CHECKS = {c.name: c for c in [
    Check("profile", "Customer profile (KYC)", profile, "context"),
    Check("volume", "Volume vs expected (KYC + core banking)", volume, "context"),
    Check("structuring", "Structuring pattern scan (core banking)", structuring, "risk"),
    Check("pass_through", "Pass-through / layering check (core banking)", pass_through, "risk"),
    Check("geography", "Jurisdiction exposure (core banking + reference data)", geography, "risk"),
    Check("sanctions", "Sanctions screening: customer + counterparties", sanctions, "risk"),
    Check("adverse_media", "Adverse media match (news feed)", adverse_media, "risk"),
    Check("novel_counterparty", "Novel high-risk counterparties (core banking)", novel_counterparty, "risk"),
    Check("dormancy", "Dormancy / reactivation check (core banking)", dormancy, "risk"),
    Check("history", "Consistency with customer's own history", history_consistency, "mitigation"),
    Check("recurring_batch", "Recurring batch / payroll pattern", recurring_batch, "mitigation"),
]}
CONTEXT_CHECKS = ["profile", "volume"]
PLANNABLE = [n for n, c in CHECKS.items() if c.category != "context"]

# Baseline planner: which checks are most relevant for each triggering rule (round 1).
RULE_PLAN = {
    "CASH_NEAR_THRESHOLD": ["structuring", "history", "geography"],
    "LARGE_CASH": ["structuring", "history"],
    "SANCTIONS_NAME_MATCH": ["sanctions", "history", "geography", "adverse_media"],
    "RAPID_MOVEMENT": ["pass_through", "geography", "sanctions", "novel_counterparty"],
    "LARGE_WIRE_OUT": ["novel_counterparty", "history", "adverse_media", "geography"],
    "LARGE_WIRE_IN": ["novel_counterparty", "history", "adverse_media", "geography"],
    "DORMANT_REACTIVATION": ["dormancy", "geography", "novel_counterparty"],
    "HIGH_VELOCITY": ["recurring_batch", "history", "pass_through"],
    "NEW_ACCOUNT_ACTIVITY": ["pass_through", "geography", "novel_counterparty"],
}
