#!/usr/bin/env python3
"""Register the AlertWhy databases in your LOCAL Zetaris and verify them. DRY-RUN by default.

    python scripts/zetaris_setup.py            # shows the statements (password masked), changes nothing
    python scripts/zetaris_setup.py --apply    # runs them (one statement per REST call, as AGENTS.md requires)
    python scripts/zetaris_setup.py all        # alias for --apply (UI and older docs)
    python scripts/zetaris_setup.py --apply --skip-exists   # safe to re-run

Statements are Zetaris Lightning SQL, taken from the getting-started repo's SQL command reference:
  CREATE DATASOURCE <name> DESCRIBE BY "..." OPTIONS (jdbcdriver ..., jdbcurl ..., username ..., password ..., fetchsize ...)
  REGISTER DATASOURCE TABLES FROM <name>
Needs .env: ZETARIS_REST_URL, ZETARIS_ORG_ID, ZETARIS_API_KEY, ALERTWHY_PG_PASSWORD (and optionally
ALERTWHY_PG_HOST_FOR_ZETARIS / ALERTWHY_PG_PORT). Never prints the API key or the password.
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from alertwhy import config  # noqa: E402
from alertwhy.federation import DEFAULT_TABLE_MAP, FederationError, ZetarisFederation  # noqa: E402

SOURCES = [("core_pg", "core", "AlertWhy core banking (accounts, transactions, alerts)"),
           ("kyc_pg", "kyc", "AlertWhy KYC customer profiles"),
           ("screen_pg", "screen", "AlertWhy screening data (sanctions, adverse media, jurisdictions)")]


def statements(password: str, mask: bool):
    pw = "********" if mask else password
    out = []
    for name, db, desc in SOURCES:
        url = f"jdbc:postgresql://{config.PG_HOST_FOR_ZETARIS}:{config.PG_PORT}/{db}"
        out.append((f"create {name}",
                    f'CREATE DATASOURCE {name} DESCRIBE BY "{desc}" OPTIONS (jdbcdriver "org.postgresql.Driver", '
                    f'jdbcurl "{url}", username "{config.PG_USER}", password "{pw}", fetchsize "1000")'))
        out.append((f"register tables of {name}", f"REGISTER DATASOURCE TABLES FROM {name}"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default=None,
                    help="'all' is an alias for --apply")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--skip-exists", action="store_true")
    a = ap.parse_args()
    if a.command == "all":
        a.apply = True
    elif a.command:
        sys.exit(f"Unknown command {a.command!r}. Use --apply, or the alias 'all'.")
    if not config.PG_PASSWORD:
        sys.exit("Set ALERTWHY_PG_PASSWORD in .env (the same value docker-compose uses).")
    try:
        z = ZetarisFederation()
    except FederationError as e:
        sys.exit(f"Config problem: {e}")
    if not a.apply:
        print("DRY RUN (nothing is changed). Statements that would run, in order:\n")
        for label, sql in statements(config.PG_PASSWORD, mask=True):
            print(f"-- {label}\n{sql}\n")
        print("Re-run with --apply to execute them.")
        return
    failures = 0
    for label, sql in statements(config.PG_PASSWORD, mask=False):
        try:
            z.run_raw(sql)
            print(f"OK   {label}")
        except FederationError as e:
            msg = str(e)
            msg = msg.replace(config.PG_PASSWORD, "********")
            if a.skip_exists and re.search(r"(?i)already\s+exist", msg):
                print(f"SKIP {label} (already exists)")
            else:
                failures += 1
                print(f"FAIL {label}: {msg[:300]}")
    print("\nVerifying logical tables through the same path the agent uses:")
    for logical, physical in DEFAULT_TABLE_MAP.items():
        try:
            n = z.query(f"SELECT COUNT(*) AS cnt FROM {logical}")[0]["cnt"]
            print(f"PASS {logical:<18} -> {physical:<26} {n} rows")
        except FederationError as e:
            failures += 1
            print(f"FAIL {logical:<18} -> {physical}: {str(e)[:200]}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
