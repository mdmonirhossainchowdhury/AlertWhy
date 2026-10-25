"""Generate a fully SYNTHETIC bank spread over five fragmented sources.

Sources (deliberately different formats, as in a real bank):
  core_banking.duckdb   accounts, transactions, alerts   (operational DB)
  kyc_customers.parquet customer KYC profiles            (data lake)
  sanctions_list.csv    watchlist entries                (vendor file)
  adverse_media.jsonl   negative-news records            (news feed)
  jurisdictions.csv     illustrative country risk tiers  (reference data)

All names are fictional. Country tiers are ILLUSTRATIVE, not an official list.
ground_truth.csv is written to data/eval/ and is NEVER exposed to the agent.
"""
import csv
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

from . import config

SEED = 42
FIRST = ["Amara", "Liam", "Sofia", "Noah", "Mei", "Omar", "Elena", "Raj", "Chloe", "Diego", "Fatima", "Hugo",
         "Ingrid", "Kenji", "Lucia", "Mateo", "Nadia", "Oscar", "Priya", "Quinn", "Rosa", "Sven", "Tara", "Umar"]
LAST = ["Alvarez", "Brandt", "Chen", "Dubois", "Eriksen", "Fofana", "Gupta", "Haddad", "Ivanova", "Jensen",
        "Kowalski", "Lindqvist", "Mbeki", "Novak", "Okafor", "Petrov", "Quispe", "Rossi", "Silva", "Tanaka"]
BIZ_A = ["Harbor", "Summit", "Cedar", "Blue Ridge", "Granite", "Lumen", "Atlas", "Pioneer", "Coastal", "Evergreen"]
BIZ_B = ["Foods", "Textiles", "Components", "Consulting", "Logistics", "Printing", "Hardware", "Dental Supply"]
COUNTRIES_OK = ["US", "GB", "DE", "CA", "AU", "SG", "FR", "NL", "JP"]
JURIS = [  # country, tier, note  (illustrative)
    ("US", "low"), ("GB", "low"), ("DE", "low"), ("CA", "low"), ("AU", "low"), ("SG", "low"), ("FR", "low"),
    ("NL", "low"), ("JP", "low"), ("AE", "medium"), ("TR", "medium"), ("BR", "medium"), ("NG", "medium"),
    ("PA", "elevated"), ("KY", "elevated"), ("VG", "elevated"), ("RU", "elevated"),
    ("IR", "high"), ("KP", "high"), ("MM", "high"),
]

AS_OF = date.fromisoformat(config.AS_OF)


def _bulk(con, table, rows):
    """Fast bulk load (executemany is row-by-row and takes ~15s for this data)."""
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="") as f:
            w = csv.writer(f)
            for r in rows:
                w.writerow(["" if v is None else v for v in r])
        con.execute(f"COPY {table} FROM '{path}' (FORMAT CSV, HEADER false, NULLSTR '')")
    finally:
        os.unlink(path)


def _write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])


def export_pg_csv(data_dir, accounts, txns, alerts, customers, sanctions, juris, media):
    """CSV files for the Postgres container (docker/pg-init.sql loads them). Three databases = three separate
    systems for Zetaris to federate: core (banking), kyc (customer data), screening (watchlists + news)."""
    out = Path(data_dir) / "pg_csv"
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(out / "accounts.csv", ["account_id", "customer_id", "opened", "status"], accounts)
    _write_csv(out / "transactions.csv", ["txn_id", "account_id", "customer_id", "ts", "direction", "amount_usd",
                                          "channel", "counterparty_name", "counterparty_country"], txns)
    _write_csv(out / "alerts.csv", ["alert_id", "customer_id", "rule_id", "rule_desc", "triggered_at",
                                    "trigger_txn_ids"], alerts)
    _write_csv(out / "customers.csv", ["customer_id", "name", "dob", "country", "kind", "occupation", "onboarded",
                                       "expected_monthly_usd", "risk_rating"], customers)
    _write_csv(out / "sanctions.csv", ["entity_id", "name", "aliases", "dob", "country", "list_name"], sanctions)
    _write_csv(out / "jurisdictions.csv", ["country", "risk_tier"], juris)
    _write_csv(out / "adverse_media.csv", ["entity", "country", "date", "headline", "category", "severity", "source"],
               [(m["entity"], m["country"], m["date"], m["headline"], m["category"], m["severity"], m["source"])
                for m in media])


def _ts(d, rnd, hour=None):
    h = rnd.randint(8, 18) if hour is None else hour
    return datetime(d.year, d.month, d.day, h, rnd.randint(0, 59))


def build(data_dir: Path = None, seed: int = SEED) -> Path:
    data_dir = Path(data_dir or config.DATA_DIR)
    (data_dir / "eval").mkdir(parents=True, exist_ok=True)
    rnd = random.Random(seed)

    customers, accounts, txns, alerts, truth, media = [], [], [], [], [], []
    tid = [0]

    def add_txn(cid, acct, when, direction, amount, channel, cp_name, cp_country):
        tid[0] += 1
        t = f"T{tid[0]:06d}"
        txns.append((t, acct, cid, when, direction, round(amount, 2), channel, cp_name, cp_country))
        return t

    def add_customer(cid, name, dob, country, kind, occupation, onboarded, expected, risk="low"):
        customers.append((cid, name, dob, country, kind, occupation, onboarded, expected, risk))
        accounts.append((f"AC{cid[1:]}", cid, onboarded, "open"))
        return f"AC{cid[1:]}"

    # ---------------- baseline population ----------------
    cp_pool = [(f"{rnd.choice(BIZ_A)} {rnd.choice(BIZ_B)} Ltd", rnd.choice(COUNTRIES_OK)) for _ in range(40)]
    for i in range(1, 281):
        cid = f"C{i:04d}"
        biz = i > 240
        if biz:
            name = f"{rnd.choice(BIZ_A)} {rnd.choice(BIZ_B)} {i} Inc"
            dob, kind, occ = None, "business", "operating company"
            expected = rnd.choice([40000, 80000, 150000])
        else:
            name = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
            dob = date(rnd.randint(1955, 2000), rnd.randint(1, 12), rnd.randint(1, 28)).isoformat()
            kind, occ = "individual", rnd.choice(["engineer", "teacher", "nurse", "driver", "designer", "clerk"])
            expected = rnd.choice([3000, 5000, 8000])
        onboarded = (AS_OF - timedelta(days=rnd.randint(400, 3000))).isoformat()
        ctry = rnd.choice(COUNTRIES_OK)
        acct = add_customer(cid, name, dob, ctry, kind, occ, onboarded, expected)
        # 12 months of history
        for m in range(12):
            base = AS_OF - timedelta(days=30 * m + rnd.randint(0, 10))
            if biz:
                cp = rnd.choice(cp_pool[:6])
                add_txn(cid, acct, _ts(base, rnd), "IN", expected * rnd.uniform(0.3, 0.7), "wire", cp[0], cp[1])
                add_txn(cid, acct, _ts(base - timedelta(days=3), rnd), "OUT", expected * rnd.uniform(0.2, 0.6), "wire",
                        rnd.choice(cp_pool[:6])[0], "US")
            else:
                add_txn(cid, acct, _ts(base.replace(day=1) if base.day > 1 else base, rnd), "IN",
                        expected * rnd.uniform(0.7, 1.0), "ach", "Employer Payroll Co", ctry)
                for _ in range(rnd.randint(2, 5)):
                    add_txn(cid, acct, _ts(base - timedelta(days=rnd.randint(0, 25)), rnd), "OUT",
                            expected * rnd.uniform(0.02, 0.2), "card", rnd.choice(cp_pool)[0], ctry)

    # ---------------- sanctions + jurisdictions ----------------
    sanctions = [
        ("S001", "Viktor Andreyev Morozov", "Viktor Morozov;V. A. Morozov", "1971-03-14", "RU", "Synthetic-Watchlist-A"),
        ("S002", "Karim Nasser Haddadi", "K. N. Haddadi", "1965-11-02", "IR", "Synthetic-Watchlist-A"),
        ("S003", "Orion Meridian Holdings", "Orion Meridian", "", "VG", "Synthetic-Watchlist-B"),
        ("S004", "Sunrise Valley Trading", "", "", "MM", "Synthetic-Watchlist-B"),
    ]
    with open(data_dir / "sanctions_list.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["entity_id", "name", "aliases", "dob", "country", "list_name"])
        w.writerows(sanctions)
    with open(data_dir / "jurisdictions.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["country", "risk_tier"])
        w.writerows(JURIS)

    # ---------------- planted alert scenarios ----------------
    def alert(aid, cid, rule, desc, when, tx_ids, label, why):
        alerts.append((aid, cid, rule, desc, when, ",".join(tx_ids)))
        truth.append((aid, label, why))

    d = lambda n: AS_OF - timedelta(days=n)  # noqa: E731

    # A-1001 structuring (true positive)
    acct = add_customer("C9001", "Daniel Whitlock", "1984-05-21", "US", "individual", "bartender",
                        (AS_OF - timedelta(days=700)).isoformat(), 4000)
    ids = [add_txn("C9001", acct, _ts(d(14 - i), rnd), "IN", rnd.uniform(9000, 9900), "cash", "CASH DEPOSIT", "US")
           for i in range(9)]
    alert("A-1001", "C9001", "CASH_NEAR_THRESHOLD", "Repeated cash deposits just under reporting threshold",
          _ts(d(5), rnd), ids[-3:], "ESCALATE", "structuring")

    # A-1002 sanctions strong match (true positive)
    acct = add_customer("C9002", "Viktor A. Morozov", "1971-03-14", "RU", "individual", "consultant",
                        (AS_OF - timedelta(days=200)).isoformat(), 6000)
    ids = [add_txn("C9002", acct, _ts(d(20 - 3 * i), rnd), "IN", rnd.uniform(15000, 30000), "wire",
                   "Orion Meridian Holdings", "VG") for i in range(4)]
    alert("A-1002", "C9002", "SANCTIONS_NAME_MATCH", "Customer name resembles a watchlist entry",
          _ts(d(3), rnd), ids[:1], "ESCALATE", "sanctions + offshore")

    # A-1003 sanctions false positive (same name, different person)
    acct = add_customer("C9003", "Viktor Morozov", "1989-07-02", "CA", "individual", "dentist",
                        (AS_OF - timedelta(days=1800)).isoformat(), 9000)
    ids = []
    for m in range(12):
        b = d(30 * m + 2)
        ids.append(add_txn("C9003", acct, _ts(b, rnd), "IN", rnd.uniform(7500, 9500), "ach", "Maple Dental Group", "CA"))
        add_txn("C9003", acct, _ts(b - timedelta(days=5), rnd), "OUT", rnd.uniform(500, 2500), "card",
                "Grocery & Fuel", "CA")
    alert("A-1003", "C9003", "SANCTIONS_NAME_MATCH", "Customer name resembles a watchlist entry",
          _ts(d(2), rnd), ids[:1], "CLOSE", "name-only match; DOB/country differ; stable history")

    # A-1004 layering / pass-through shell (true positive)
    acct = add_customer("C9004", "Zenith Bridge Trading LLC", None, "AE", "business", "trading",
                        (AS_OF - timedelta(days=75)).isoformat(), 20000)
    ids = []
    for i, (cp, cc, amt) in enumerate([("Alder Imports GmbH", "DE", 210000), ("Kestrel Partners SA", "PA", 195000),
                                       ("Lotus Global Ltd", "KY", 205000)]):
        ids.append(add_txn("C9004", acct, _ts(d(9 - 2 * i), rnd, 10), "IN", amt, "wire", cp, cc))
        add_txn("C9004", acct, _ts(d(9 - 2 * i), rnd, 16), "OUT", amt * 0.97, "wire",
                ["Sunrise Valley Trading", "Mirage Exports FZE", "Tehran Bridge Co"][i], ["MM", "AE", "IR"][i])
    alert("A-1004", "C9004", "RAPID_MOVEMENT", "Funds in and out within hours at unusual volume",
          _ts(d(1), rnd), ids, "ESCALATE", "layering through new entity to high-risk jurisdictions")

    # A-1005 adverse media on business
    acct = add_customer("C9005", "Northwind Logistics Inc", None, "US", "business", "freight",
                        (AS_OF - timedelta(days=900)).isoformat(), 120000)
    ids = [add_txn("C9005", acct, _ts(d(30 * m + 4), rnd), "IN", rnd.uniform(60000, 110000), "wire",
                   "Cedar Foods Ltd", "US") for m in range(10)]
    ids.append(add_txn("C9005", acct, _ts(d(4), rnd), "OUT", 480000, "wire", "Kestrel Partners SA", "PA"))
    media.append({"entity": "Northwind Logistics Inc", "country": "US", "date": (AS_OF - timedelta(days=95)).isoformat(),
                  "headline": "Freight firm Northwind Logistics under investigation over bribery of port officials",
                  "category": "bribery", "severity": "high", "source": "Synthetic Business Wire"})
    alert("A-1005", "C9005", "LARGE_WIRE_OUT", "Unusually large outbound wire", _ts(d(3), rnd), ids[-1:],
          "ESCALATE", "adverse media + large offshore wire out of pattern")

    # A-1006 benign established manufacturer
    acct = add_customer("C9006", "Granite Ridge Components Inc", None, "US", "business", "manufacturing",
                        (AS_OF - timedelta(days=2900)).isoformat(), 400000)
    ids = []
    for m in range(24):
        ids.append(add_txn("C9006", acct, _ts(d(30 * m + 6), rnd), "IN", rnd.uniform(380000, 520000), "wire",
                           "Pioneer Hardware Ltd", "US"))
    alert("A-1006", "C9006", "LARGE_WIRE_IN", "Unusually large inbound wire", _ts(d(5), rnd), ids[:1],
          "CLOSE", "consistent with 24 months of identical customer payments")

    # A-1007 dormant account suddenly active
    acct = add_customer("C9007", "Ingrid Solheim", "1990-02-11", "NO", "individual", "student",
                        (AS_OF - timedelta(days=1500)).isoformat(), 1500)
    add_txn("C9007", acct, _ts(d(520), rnd), "IN", 800, "ach", "Part-time Employer", "NO")
    ids = []
    for i in range(5):
        ids.append(add_txn("C9007", acct, _ts(d(10 - i), rnd), "IN", rnd.uniform(18000, 26000), "wire",
                           rnd.choice(["Lotus Global Ltd", "Kestrel Partners SA"]), rnd.choice(["KY", "PA"])))
    add_txn("C9007", acct, _ts(d(4), rnd), "OUT", 98000, "wire", "NovaCrypto Exchange", "VG")
    alert("A-1007", "C9007", "DORMANT_REACTIVATION", "Dormant account reactivated with large inflows",
          _ts(d(4), rnd), ids, "ESCALATE", "dormancy + elevated-risk inflows + crypto exit")

    # A-1008 payroll batch (false positive)
    acct = add_customer("C9008", "Evergreen Dental Supply Inc", None, "US", "business", "distribution",
                        (AS_OF - timedelta(days=2200)).isoformat(), 90000)
    ids = []
    for m in range(8):
        day = d(30 * m + 1)
        for k in range(14):
            t = add_txn("C9008", acct, _ts(day, rnd, 9), "OUT", rnd.uniform(1800, 3200), "ach", f"Employee {k + 1}", "US")
            if m == 0:
                ids.append(t)
        add_txn("C9008", acct, _ts(day - timedelta(days=2), rnd), "IN", rnd.uniform(60000, 90000), "wire",
                "Coastal Foods Ltd", "US")
    alert("A-1008", "C9008", "HIGH_VELOCITY", "Many outgoing payments in one day", _ts(d(1), rnd), ids[:5],
          "CLOSE", "recurring monthly payroll batch")

    # A-1009 ambiguous new customer
    acct = add_customer("C9009", "Tomas Rieder", "1993-09-30", "DE", "individual", "freelancer",
                        (AS_OF - timedelta(days=100)).isoformat(), 3000)
    ids = [add_txn("C9009", acct, _ts(d(12), rnd), "IN", 14000, "wire", "Atlas Consulting Ltd", "TR"),
           add_txn("C9009", acct, _ts(d(8), rnd), "OUT", 6000, "wire", "Quick Remit PA", "PA")]
    alert("A-1009", "C9009", "NEW_ACCOUNT_ACTIVITY", "High activity shortly after onboarding", _ts(d(7), rnd),
          ids, "REVIEW", "mixed signals, thin history")

    # filler low-risk alerts on baseline customers (should close)
    for n in range(10):
        cid = f"C{rnd.randint(241, 280):04d}"  # cash-handling small businesses
        acct = f"AC{cid[1:]}"
        t = add_txn(cid, acct, _ts(d(rnd.randint(1, 10)), rnd), "IN", rnd.uniform(10500, 12500), "cash",
                    "CASH DEPOSIT", "US")
        alert(f"A-11{n:02d}", cid, "LARGE_CASH", "Cash deposit above reporting threshold", _ts(d(1), rnd), [t],
              "CLOSE", "single isolated cash deposit")

    # background adverse media noise (unrelated entities)
    for nm in ["Harbor Textiles Group", "Pioneer Printing Co"]:
        media.append({"entity": nm, "country": "US", "date": (AS_OF - timedelta(days=400)).isoformat(),
                      "headline": f"{nm} fined over minor labelling violation", "category": "regulatory",
                      "severity": "low", "source": "Synthetic Daily"})

    # ---------------- write everything ----------------
    db_path = data_dir / "core_banking.duckdb"
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE accounts(account_id VARCHAR, customer_id VARCHAR, opened DATE, status VARCHAR)")
    _bulk(con, "accounts", accounts)
    con.execute("""CREATE TABLE transactions(txn_id VARCHAR, account_id VARCHAR, customer_id VARCHAR, ts TIMESTAMP,
                   direction VARCHAR, amount_usd DOUBLE, channel VARCHAR, counterparty_name VARCHAR,
                   counterparty_country VARCHAR)""")
    _bulk(con, "transactions", txns)
    con.execute("""CREATE TABLE alerts(alert_id VARCHAR, customer_id VARCHAR, rule_id VARCHAR, rule_desc VARCHAR,
                   triggered_at TIMESTAMP, trigger_txn_ids VARCHAR)""")
    _bulk(con, "alerts", alerts)
    con.close()

    k = duckdb.connect()
    k.execute("""CREATE TABLE c(customer_id VARCHAR, name VARCHAR, dob VARCHAR, country VARCHAR, kind VARCHAR,
                 occupation VARCHAR, onboarded DATE, expected_monthly_usd DOUBLE, risk_rating VARCHAR)""")
    _bulk(k, "c", customers)
    k.execute(f"COPY c TO '{data_dir / 'kyc_customers.parquet'}' (FORMAT PARQUET)")
    k.close()

    with open(data_dir / "adverse_media.jsonl", "w") as f:
        for m in media:
            f.write(json.dumps(m) + "\n")
    with open(data_dir / "eval" / "ground_truth.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["alert_id", "label", "why"])
        w.writerows(truth)
    export_pg_csv(data_dir, accounts, txns, alerts, customers, sanctions, JURIS, media)
    return data_dir


if __name__ == "__main__":
    p = build()
    print(f"Synthetic data written to {p}")
