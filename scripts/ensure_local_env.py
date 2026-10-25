#!/usr/bin/env python3
"""Create a local .env from .env.example if missing. Never prints secret values."""
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parent.parent
example = ROOT / ".env.example"
env_path = ROOT / ".env"

PG_PASSWORD = "alertwhy-local-" + secrets.token_hex(6)


def upsert(text: str, key: str, value: str) -> str:
    lines, found = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"#{key}="):
            lines.append(f"{key}={value}")
            found = True
        else:
            lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def main():
    if env_path.exists():
        print(".env already exists; leaving it unchanged.")
        return
    text = example.read_text(encoding="utf-8")
    text = upsert(text, "ALERTWHY_PG_PASSWORD", PG_PASSWORD)
    text = upsert(text, "ZETARIS_REST_URL", "http://localhost:8888/api/v1.0")
    text = upsert(text, "ZETARIS_ORG_ID", "1")
    text = upsert(text, "METERLESS_BASE_URL", "http://127.0.0.1:8088/v1")
    env_path.write_text(text, encoding="utf-8")
    print("Wrote .env from .env.example with a local Postgres password.")
    print("Fill ZETARIS_API_KEY and METERLESS_RUNTIME_TOKEN yourself. Do not paste them into chat.")


if __name__ == "__main__":
    main()
