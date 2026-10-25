"""Read-only SQL guard for any SQL written by a language model.

The agent may explore with `run_sql`, but only SELECT statements over the allow-listed logical tables,
one statement, no comments, row-limited. A rejected statement returns a message the model can learn from.
"""
import re

LOGICAL_TABLES = ["core_accounts", "core_transactions", "core_alerts", "kyc", "sanctions", "media", "jurisdictions"]
MAX_LIMIT = 200
DEFAULT_LIMIT = 50

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|truncate|grant|revoke|merge|call|exec|execute|copy|attach|"
    r"pragma|set|register|upsert|refresh|load|unload|export|import|use|cache|uncache|activate|compile)\b", re.I)
_DANGEROUS = re.compile(
    r"\b(read_\w+|glob|parquet_scan|csv_scan|json_scan|sniff_csv|duckdb_\w+|information_schema|pg_\w+|"
    r"current_setting|getenv|system|shell|http\w*|s3|httpfs|load_extension|install|lateral|unnest|explode|"
    r"input_file_name|java_method|reflect)\b", re.I)
_REL = re.compile(r"\b(?:from|join)\s+([A-Za-z_][\w.]*)", re.I)


class SQLGuardError(ValueError):
    pass


def _strip_quoted(sql: str) -> str:
    return re.sub(r"'(?:[^'\\]|\\.|'')*'", "''", sql)


def validate(sql: str) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise SQLGuardError("Empty SQL.")
    s = sql.strip()
    if s.endswith(";"):
        s = s[:-1].rstrip()
    bare = _strip_quoted(s)
    if ";" in bare:
        raise SQLGuardError("Only one statement is allowed (no semicolons).")
    if "--" in bare or "/*" in bare:
        raise SQLGuardError("SQL comments are not allowed.")
    if not re.match(r"(?is)^\s*(select|with)\b", bare):
        raise SQLGuardError("Only SELECT queries are allowed.")
    bad = _FORBIDDEN.search(bare)
    if bad:
        raise SQLGuardError(f"Keyword '{bad.group(1)}' is not allowed: read-only access only.")
    bad = _DANGEROUS.search(bare)
    if bad:
        raise SQLGuardError(f"'{bad.group(1)}' is not allowed: only plain SELECTs over the listed tables.")
    if re.search(r"(?i)\bfrom\s+[\w.]+(\s+(?:as\s+)?\w+)?\s*,", bare):
        raise SQLGuardError("Comma joins are not allowed: use explicit JOIN ... ON.")
    rels = _REL.findall(bare)
    if not rels:
        raise SQLGuardError("Query must read from one of: " + ", ".join(LOGICAL_TABLES))
    ctes = {m.lower() for m in re.findall(r"(?i)\b([A-Za-z_]\w*)\s+as\s*\(", bare)}
    for r in rels:
        if r.lower() not in LOGICAL_TABLES and r.lower() not in ctes:
            raise SQLGuardError(f"Unknown table '{r}'. Allowed tables: " + ", ".join(LOGICAL_TABLES))
    m = re.search(r"(?i)\blimit\s+(\d+)\s*$", bare)
    if m:
        if int(m.group(1)) > MAX_LIMIT:
            s = re.sub(r"(?i)\blimit\s+\d+\s*$", f"LIMIT {MAX_LIMIT}", s)
    elif re.search(r"(?i)\blimit\b", bare):
        raise SQLGuardError("LIMIT must be the final clause with a plain number.")
    else:
        s = f"{s} LIMIT {DEFAULT_LIMIT}"
    return s
