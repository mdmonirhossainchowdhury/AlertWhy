"""Central configuration. Values come from environment variables or a local `.env` file (never committed).

Variable names follow the hackathon Participant Setup Guide:
  ZETARIS_REST_URL / ZETARIS_ORG_ID / ZETARIS_API_KEY        (Data layer: Zetaris)
  METERLESS_BASE_URL / METERLESS_RUNTIME_TOKEN / METERLESS_MODEL   (Power layer: Meterless Nightshift runtime)
  MODEL_BASE_URL / MODEL_API_KEY / NEMOTRON_MODEL            (optional secondary provider, e.g. NVIDIA free API)
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Tiny .env reader: KEY=VALUE lines, # comments, optional quotes. Does not override real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)


_load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("ALERTWHY_DATA_DIR", ROOT / "data"))
AS_OF = os.getenv("ALERTWHY_AS_OF", "2026-10-15")  # "today" inside the synthetic bank

# ---------------- Zetaris (federated data layer) ----------------
ZETARIS_REST_URL = os.getenv("ZETARIS_REST_URL", "").strip()          # e.g. http://localhost:8888/api/v1.0
ZETARIS_ORG_ID = os.getenv("ZETARIS_ORG_ID", "1").strip()
ZETARIS_API_KEY = os.getenv("ZETARIS_API_KEY", "").strip()
ZETARIS_QUERY_LIMIT = int(os.getenv("ZETARIS_QUERY_LIMIT", "5000"))
ZETARIS_TIMEOUT = float(os.getenv("ZETARIS_TIMEOUT", "60"))
# Logical table -> Zetaris qualified table. Override with ZETARIS_TABLE_MAP='{"kyc":"mydb.customers"}'
ZETARIS_TABLE_MAP = json.loads(os.getenv("ZETARIS_TABLE_MAP", "{}") or "{}")
# "auto" = Zetaris when ZETARIS_REST_URL and ZETARIS_API_KEY are set, else local DuckDB fallback. Or force "zetaris"/"local".
FEDERATION_BACKEND = os.getenv("FEDERATION_BACKEND", "auto").lower()

# Where the demo Postgres lives, as seen from Zetaris (Docker). host.docker.internal works on Docker Desktop;
# on Linux use the host's Docker bridge IP (often 172.17.0.1).
PG_HOST_FOR_ZETARIS = os.getenv("ALERTWHY_PG_HOST_FOR_ZETARIS", "host.docker.internal")
PG_HOST = os.getenv("ALERTWHY_PG_HOST", "localhost")           # as seen from this machine
PG_PORT = int(os.getenv("ALERTWHY_PG_PORT", "5433"))
# Port as seen from the Zetaris container. Same as PG_PORT via host.docker.internal; 5432 if the Postgres container
# is joined to Zetaris's Docker network (`python scripts/zetaris_setup.py join-network <network>`, host alertwhy-pg).
PG_PORT_FOR_ZETARIS = int(os.getenv("ALERTWHY_PG_PORT_FOR_ZETARIS", str(PG_PORT)))
PG_USER = os.getenv("ALERTWHY_PG_USER", "alertwhy")
PG_PASSWORD = os.getenv("ALERTWHY_PG_PASSWORD", "")

# ---------------- Meterless Nightshift runtime (primary AI provider) ----------------
METERLESS_BASE_URL = os.getenv("METERLESS_BASE_URL", "").strip()      # e.g. http://127.0.0.1:8088/v1
METERLESS_RUNTIME_TOKEN = os.getenv("METERLESS_RUNTIME_TOKEN", "").strip()
METERLESS_MODEL = os.getenv("METERLESS_MODEL", "meterless/auto").strip()
METERLESS_APP = os.getenv("METERLESS_APP", "alertwhy").strip()
METERLESS_TIMEOUT = float(os.getenv("METERLESS_TIMEOUT", "180"))

# Optional secondary provider (any OpenAI-compatible API, e.g. NVIDIA's free endpoint for Nemotron).
MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "").strip()
MODEL_API_KEY = os.getenv("MODEL_API_KEY", "").strip()
NEMOTRON_MODEL = os.getenv("NEMOTRON_MODEL", "").strip()

# ---------------- H-MEM engine (Meterless Engines) ----------------
HMEM_ENABLED = os.getenv("HMEM_ENABLED", "true").lower() not in ("0", "false", "no")
HMEM_DIR = Path(os.getenv("HMEM_DIR", DATA_DIR / "hmem"))
NODE_BIN = os.getenv("NODE_BIN", "node")

# ---------------- Agent loop ----------------
MAX_ROUNDS = int(os.getenv("MAX_ROUNDS", "3"))
EXPLORE_MAX_QUERIES = int(os.getenv("EXPLORE_MAX_QUERIES", "3"))

# ---------------- Cost model (ASSUMPTIONS for an estimate, not billing data) ----------------
COST_PER_QUERY_USD = float(os.getenv("COST_PER_QUERY_USD", "0.0002"))
COST_PER_1K_TOKENS_USD = float(os.getenv("COST_PER_1K_TOKENS_USD", "0.002"))
MAX_COST_PER_INVESTIGATION_USD = float(os.getenv("MAX_COST_PER_INVESTIGATION_USD", "0.05"))
MANUAL_MINUTES_PER_ALERT = float(os.getenv("MANUAL_MINUTES_PER_ALERT", "25"))
ANALYST_HOURLY_USD = float(os.getenv("ANALYST_HOURLY_USD", "45"))

# ---------------- Decision thresholds ----------------
ESCALATE_AT = int(os.getenv("ESCALATE_AT", "60"))
CLOSE_AT = int(os.getenv("CLOSE_AT", "20"))
