# How AlertWhy uses the sponsor technologies

## Zetaris: the data layer
**What it does here.** Every number the agent uses comes from a federated SQL query. Seven logical tables
(accounts, transactions, alerts, KYC, sanctions, adverse media, jurisdictions) map to three Zetaris datasources
(`core_pg`, `kyc_pg`, `screen_pg`), each backed by a Postgres database. The agent never reads files directly.

**Why Zetaris and not a local database.** The bank's data lives in several systems. Zetaris lets one SQL
statement join across them in place, without copying the data into a warehouse first. That is the
capability AlertWhy needs: a single investigation joins the customer's KYC record, their transactions and a
sanctions list.

**How it is called.** `alertwhy/federation.py` posts one statement at a time to `POST /sql-editor/sqls/run`
on the REST port (8888), with a Bearer key, `X-Org-ID` and a fresh `X-Request-ID` per call. Table names are
rewritten to the datasource-qualified names. Every statement the agent runs is saved in the investigation's
audit trail with the exact Zetaris SQL that was sent.

**Safety.** AI-written SQL passes a read-only guard (`alertwhy/sqlguard.py`) before it reaches Zetaris: SELECT
only, one statement, no comments, allow-listed tables and a row limit.

**Setup.** `docker-compose.yml` runs the sample Postgres. `scripts/zetaris_setup.py` (dry-run by default)
creates the datasources and registers their tables. The API key is created by you in the Zetaris web UI and
stored in your local `.env`.

## Meterless: the AI runtime and the memory engine
**Nightshift as the AI provider.** Planning, the critic, guarded exploration SQL and narration all go through
the Meterless Nightshift runtime, which is OpenAI-compatible. AlertWhy calls `/v1/chat/completions` with the
runtime token and the `meterless-app` and `meterless-scope: user` headers. Model ids are routing modes
(`meterless/auto`), and the router reports the real model that answered.

**Why Nightshift.** Routing many models behind one runtime token means the agent code does not change when
the model does. It also gives one place to see which model answered each call.

**Fallback.** If Nightshift is offline, the agent runs on rules only and says so in the limitations list. A
bad token is never silently replaced by another provider. Transient failures (connection errors, 429, 5xx)
are retried up to three times in total.

**H-MEM engine for working memory.** H-MEM is Meterless's memory engine (Apache-2.0, vendored unmodified in
`engines/hmem`). AlertWhy uses it so that a later alert about the same customer or counterparty can recall
earlier cases:
- Recall shows earlier cases as evidence. Only analyst decisions can move the score, capped at +10 risk or -10
  mitigation in total, so the agent's own past verdicts cannot reinforce themselves.
- An analyst who overturns a decision supersedes the old memory. The old record is kept and penalised in
  ranking, not deleted, and the change is written to the engine's trust ledger.
- The engine passes its own conformance suite (13/13, `engines/hmem/conformance`), and AlertWhy's tests run it.

## NVIDIA Nemotron (optional)
An OpenAI-compatible secondary route (`MODEL_BASE_URL`, `NEMOTRON_MODEL`) can serve Nemotron. It is used only
when Nightshift is not configured.

## What is verified and what is not
- Verified here: the Zetaris contract (against a fake that enforces the setup guide's request shape), the
  Nightshift provider (against a fake server), the H-MEM conformance suite, and the 114 tests.
- Not verified here: a live Zetaris instance and a live Nightshift runtime. Run
  `python -m alertwhy.cli status` once both are configured.
