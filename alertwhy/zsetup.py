"""Zetaris setup helpers (used by scripts/zetaris_setup.py and tests).

Why Postgres? Zetaris "can't read a file on your laptop" (setup guide, troubleshooting) and registers JDBC databases
natively (`CREATE DATASOURCE ... OPTIONS (jdbcdriver ..., jdbcurl ...)`, then `REGISTER DATASOURCE TABLES FROM ...`,
Zetaris SQL reference section 4). So the synthetic bank is loaded into a small demo Postgres, split across THREE
databases to keep the data genuinely fragmented, and each database is registered as its own Zetaris datasource:

    Postgres database   Zetaris datasource   tables
    core                core_pg              accounts, transactions, alerts
    kyc                 kyc_pg               customers
    screen              screen_pg            sanctions, adverse_media, jurisdictions

The agent then reads them as core_pg.transactions, kyc_pg.customers ... (federation.DEFAULT_TABLE_MAP) and Zetaris
joins across datasources (core_pg.alerts JOIN kyc_pg.customers).
"""
import re

import duckdb

from . import config
from .federation import FederationError, LOGICAL_TABLES, DEFAULT_TABLE_MAP

DATABASES = {
    "core": {"ds": "core_pg", "label": "core banking", "tables": {
        "accounts": ("account_id text, customer_id text, opened date, status text", "core_accounts"),
        "transactions": ("txn_id text, account_id text, customer_id text, ts timestamp, direction text, "
                         "amount_usd double precision, channel text, counterparty_name text, "
                         "counterparty_country text", "core_transactions"),
        "alerts": ("alert_id text, customer_id text, rule_id text, rule_desc text, triggered_at timestamp, "
                   "trigger_txn_ids text", "core_alerts"),
    }},
    "kyc": {"ds": "kyc_pg", "label": "KYC profiles", "tables": {
        "customers": ("customer_id text, name text, dob text, country text, kind text, occupation text, "
                      "onboarded date, expected_monthly_usd double precision, risk_rating text", "kyc"),
    }},
    "screen": {"ds": "screen_pg", "label": "screening and reference data", "tables": {
        "sanctions": ("entity_id text, name text, aliases text, dob text, country text, list_name text", "sanctions"),
        "adverse_media": ("entity text, country text, date date, headline text, category text, severity text, "
                          "source text", "media"),
        "jurisdictions": ("country text, risk_tier text", "jurisdictions"),
    }},
}

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _ident(name):
    if not _IDENT.match(name):
        raise ValueError(f"unsafe identifier {name!r}")
    return name


def _dq(value: str) -> str:
    """Double-quoted Lightning SQL option value. Quotes and backslashes are refused instead of escaped."""
    if any(c in value for c in '"\\\n\r'):
        raise ValueError("value contains a quote, backslash or newline, which Lightning SQL options cannot carry safely")
    return f'"{value}"'


def register_statements(host, port, user, password, mask=False):
    """The exact Lightning SQL sent to Zetaris, one statement per entry (no trailing semicolon)."""
    out = []
    for db, spec in DATABASES.items():
        ds = _ident(spec["ds"])
        pw = "********" if mask else password
        out.append(
            f'CREATE DATASOURCE {ds} DESCRIBE BY {_dq("AlertWhy synthetic " + spec["label"])} OPTIONS ('
            f'jdbcdriver "org.postgresql.Driver", jdbcurl {_dq(f"jdbc:postgresql://{host}:{int(port)}/{_ident(db)}")}, '
            f'username {_dq(user)}, password {_dq(pw)}, fetchsize "1000")')
        out.append(f"REGISTER DATASOURCE TABLES FROM {ds}")
    return out


def source_queries(d):
    """DuckDB SELECTs that read each Postgres table's rows from the generated files."""
    return {
        "accounts": "SELECT * FROM core.accounts",
        "transactions": "SELECT * FROM core.transactions",
        "alerts": "SELECT * FROM core.alerts",
        "customers": f"SELECT * FROM read_parquet('{d}/kyc_customers.parquet')",
        "sanctions": f"SELECT * FROM read_csv_auto('{d}/sanctions_list.csv', all_varchar=true)",
        "adverse_media": f"SELECT * FROM read_json_auto('{d}/adverse_media.jsonl', format='newline_delimited')",
        "jurisdictions": f"SELECT * FROM read_csv_auto('{d}/jurisdictions.csv')",
    }


def source_rows(data_dir=None):
    """Rows for every Postgres table, read from the generated files: {pg_table: (columns, [tuples])}."""
    d = data_dir or config.DATA_DIR
    con = duckdb.connect()
    con.execute(f"ATTACH '{d}/core_banking.duckdb' AS core (READ_ONLY)")
    out = {}
    for t, sql in source_queries(d).items():
        cur = con.execute(sql)
        cols = [c[0] for c in cur.description]
        rows = [tuple(None if v == "" else v for v in r) for r in cur.fetchall()]
        out[t] = (cols, rows)
    con.close()
    return out


def load_postgres(host, port, user, password, data_dir=None, log=print):
    """Create the three databases (if missing) and (re)load the tables. Returns {table: row_count}."""
    import psycopg  # optional dependency, only needed for this setup step

    data = source_rows(data_dir)
    counts = {}
    admin = psycopg.connect(host=host, port=port, user=user, password=password, dbname="postgres", autocommit=True)
    try:
        for db in DATABASES:
            exists = admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (db,)).fetchone()
            if not exists:
                admin.execute(f"CREATE DATABASE {_ident(db)}")
                log(f"  created database {db}")
    finally:
        admin.close()
    for db, spec in DATABASES.items():
        with psycopg.connect(host=host, port=port, user=user, password=password, dbname=db) as conn:
            for table, (ddl, _logical) in spec["tables"].items():
                _ident(table)
                conn.execute(f"DROP TABLE IF EXISTS {table}")
                conn.execute(f"CREATE TABLE {table} ({ddl})")
                cols, rows = data[table]
                with conn.cursor() as cur:
                    with cur.copy(f"COPY {table} ({', '.join(map(_ident, cols))}) FROM STDIN") as cp:
                        for r in rows:
                            cp.write_row(r)
                counts[f"{db}.{table}"] = len(rows)
                log(f"  {db}.{table}: {len(rows)} rows")
            conn.commit()
    return counts


def expected_counts(data_dir=None):
    """Row counts per LOGICAL table, from the generated files (what Zetaris should return)."""
    data = source_rows(data_dir)
    out = {}
    for spec in DATABASES.values():
        for table, (_ddl, logical) in spec["tables"].items():
            out[logical] = len(data[table][1])
    return out


def register_in_zetaris(fed, host, port, user, password, log=print):
    """Send the registration statements one at a time; 'already exists' is fine (guide: reuse and move on)."""
    done, skipped = [], []
    for sql, shown in zip(register_statements(host, port, user, password),
                          register_statements(host, port, user, password, mask=True)):
        try:
            fed.run_raw(sql)
            done.append(shown)
            log(f"  ok: {shown[:90]}")
        except FederationError as e:
            if "exist" in str(e).lower():
                skipped.append(shown)
                log(f"  already exists, reusing: {shown[:70]}")
            else:
                raise
    return done, skipped


def verify(fed, data_dir=None):
    """Compare Zetaris row counts with the source files. Returns (ok, {logical: (zetaris, expected)})."""
    exp = expected_counts(data_dir)
    result, ok = {}, True
    for t in LOGICAL_TABLES:
        try:
            got = fed.query(f"SELECT COUNT(*) AS cnt FROM {t}")[0]["cnt"]
        except FederationError as e:
            got = f"ERROR {str(e)[:100]}"
        result[t] = (got, exp[t])
        ok = ok and got == exp[t]
    return ok, result


def table_names():
    return dict(DEFAULT_TABLE_MAP)
