# Architecture

## Flow of one investigation
1. `POST /api/alerts/{id}/investigate` -> `Investigator.investigate()`
2. Load alert + KYC profile (federated queries).
3. Run the 10 tools in `tools.py`; each issues parameterised SQL through `Federation.query()` and returns evidence
   items `{id, tool, source, kind: risk|mitigating|info, points, finding, detail}`.
4. Score = clamp(sum(risk) + sum(mitigating), 0, 100).
   - `>= ESCALATE_AT (60)` or any strong sanctions match -> ESCALATE
   - `<= CLOSE_AT (20)` -> CLOSE, unless a single red flag >= 25 points exists -> REVIEW
   - otherwise REVIEW
5. Confidence from score margin and the number of independent supporting sources.
6. Explanation: deterministic template, or an LLM (if configured) constrained by a citation guard.
7. Emit a Meterless-style usage event; write `data/investigations/<alert>.json` (full audit trail incl. all SQL).

## Data sources (fragmented on purpose)
| Logical table | Physical source | Format |
|---|---|---|
| core_accounts / core_transactions / core_alerts | core_banking.duckdb | operational DB |
| kyc | kyc_customers.parquet | data lake |
| sanctions | sanctions_list.csv | vendor file |
| media | adverse_media.jsonl | news feed |
| jurisdictions | jurisdictions.csv | reference data (illustrative tiers) |

## Evidence tools
profile & volume, structuring, pass-through, jurisdiction exposure, sanctions (customer + counterparties),
adverse media, novel high-risk counterparties, dormancy, history consistency (mitigating), recurring batch (mitigating).

## Why not let the LLM decide?
Regulated decisions need reproducible, auditable logic. The LLM adds readability only, and cannot cite evidence that
does not exist (`llm.guard_ok`). Swapping the model never changes a verdict.

## Zetaris and Meterless seams
- `federation.py`: `LocalFederation` (DuckDB in-place) <-> `ZetarisFederation` (HTTP adapter, to be matched to the real API).
- `usage.py`: local JSONL ledger of queries, tokens and estimated cost for the UI. Estimates only, not billing.
- `hmem.py`: Meterless H-MEM engine (vendored, Apache-2.0) via a Node JSON-lines bridge; stores analyst memory.
