"""Export the synthetic bank as PostgreSQL init files so Zetaris can register it as JDBC datasources.

Why: Zetaris cannot read files on your laptop (Setup Guide, p.22). It CAN register JDBC databases. We therefore ship
a tiny Postgres container with THREE separate databases, one per source system, which Zetaris then federates:

    core    accounts, transactions, alerts     -> Zetaris datasource core_pg
    kyc     customers                          -> Zetaris datasource kyc_pg
    screen  sanctions, adverse_media, jurisdictions -> Zetaris datasource screen_pg

Writes data/pg_init/01_init.sql + data/pg_init/csv/*.csv. docker-compose mounts them into the Postgres init folder.
"""
from pathlib import Path

import duckdb

from . import config

DDL = """\
CREATE DATABASE core;
CREATE DATABASE kyc;
CREATE DATABASE screen;

\\connect core
CREATE TABLE accounts(account_id VARCHAR(16), customer_id VARCHAR(16), opened DATE, status VARCHAR(16));
CREATE TABLE transactions(txn_id VARCHAR(16), account_id VARCHAR(16), customer_id VARCHAR(16), ts TIMESTAMP,
  direction VARCHAR(4), amount_usd DOUBLE PRECISION, channel VARCHAR(16), counterparty_name VARCHAR(128),
  counterparty_country VARCHAR(4));
CREATE TABLE alerts(alert_id VARCHAR(16), customer_id VARCHAR(16), rule_id VARCHAR(48), rule_desc VARCHAR(160),
  triggered_at TIMESTAMP, trigger_txn_ids VARCHAR(400));
COPY accounts FROM '/csv/accounts.csv' CSV HEADER;
COPY transactions FROM '/csv/transactions.csv' CSV HEADER;
COPY alerts FROM '/csv/alerts.csv' CSV HEADER;
CREATE INDEX ON transactions(customer_id, ts);

\\connect kyc
CREATE TABLE customers(customer_id VARCHAR(16), name VARCHAR(128), dob VARCHAR(16), country VARCHAR(4),
  kind VARCHAR(16), occupation VARCHAR(48), onboarded DATE, expected_monthly_usd DOUBLE PRECISION,
  risk_rating VARCHAR(16));
COPY customers FROM '/csv/customers.csv' CSV HEADER;

\\connect screen
CREATE TABLE sanctions(entity_id VARCHAR(16), name VARCHAR(128), aliases VARCHAR(256), dob VARCHAR(16),
  country VARCHAR(4), list_name VARCHAR(48));
CREATE TABLE adverse_media(entity VARCHAR(128), country VARCHAR(4), date DATE, headline VARCHAR(256),
  category VARCHAR(32), severity VARCHAR(16), source VARCHAR(64));
CREATE TABLE jurisdictions(country VARCHAR(4), risk_tier VARCHAR(16));
COPY sanctions FROM '/csv/sanctions.csv' CSV HEADER;
COPY adverse_media FROM '/csv/adverse_media.csv' CSV HEADER;
COPY jurisdictions FROM '/csv/jurisdictions.csv' CSV HEADER;
"""


def export(data_dir: Path = None) -> Path:
    d = Path(data_dir or config.DATA_DIR)
    out = d / "pg_init"
    csvdir = out / "csv"
    csvdir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"ATTACH '{d}/core_banking.duckdb' AS core (READ_ONLY)")
    jobs = {
        "accounts": "SELECT account_id, customer_id, opened, status FROM core.accounts",
        "transactions": "SELECT txn_id, account_id, customer_id, ts, direction, amount_usd, channel, "
                        "counterparty_name, counterparty_country FROM core.transactions ORDER BY ts",
        "alerts": "SELECT alert_id, customer_id, rule_id, rule_desc, triggered_at, trigger_txn_ids FROM core.alerts",
        "customers": f"SELECT customer_id, name, dob, country, kind, occupation, onboarded, expected_monthly_usd, "
                     f"risk_rating FROM read_parquet('{d}/kyc_customers.parquet')",
        "sanctions": f"SELECT entity_id, name, aliases, dob, country, list_name FROM "
                     f"read_csv_auto('{d}/sanctions_list.csv', all_varchar=true)",
        "adverse_media": f"SELECT entity, country, CAST(date AS DATE) AS date, headline, category, severity, source "
                         f"FROM read_json_auto('{d}/adverse_media.jsonl', format='newline_delimited')",
        "jurisdictions": f"SELECT country, risk_tier FROM read_csv_auto('{d}/jurisdictions.csv')",
    }
    for name, sql in jobs.items():
        con.execute(f"COPY ({sql}) TO '{csvdir / (name + '.csv')}' (FORMAT CSV, HEADER true)")
    (out / "01_init.sql").write_text(DDL)
    return out
