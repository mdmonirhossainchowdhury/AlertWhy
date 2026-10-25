#!/usr/bin/env python3
"""Print the SHAPE (keys and types, never secrets) of real Zetaris REST responses, to confirm/adjust the parser.

    python scripts/zetaris_probe.py
AlertWhy's parser accepts rows as lists or objects and several column-name keys; if a response uses something else,
this tells you exactly what to change in ZetarisFederation.parse().
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from alertwhy.federation import FederationError, ZetarisFederation  # noqa: E402


def shape(x, depth=0):
    if isinstance(x, dict):
        return {k: shape(v, depth + 1) for k, v in list(x.items())[:12]}
    if isinstance(x, list):
        return [shape(x[0], depth + 1), f"... {len(x)} items"] if x else []
    return type(x).__name__


def main():
    z = ZetarisFederation()
    for sql in ("SELECT 1 AS one", "SELECT customer_id, name, expected_monthly_usd FROM kyc_pg.customers LIMIT 2",
                "SELECT COUNT(*) AS cnt FROM core_pg.transactions"):
        try:
            r = z.run_raw(sql)
            print(f"\n{sql}\n  shape: {json.dumps(shape(r), indent=2)}")
            parsed = z.parse(r, sql)
            print(f"  parsed OK -> {parsed[:2]}")
        except FederationError as e:
            print(f"\n{sql}\n  FAIL: {e}")


if __name__ == "__main__":
    main()
