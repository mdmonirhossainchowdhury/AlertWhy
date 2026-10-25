# How Cursor was used

Project rules: `.cursor/rules/alertwhy.mdc`. All hackathon coding for AlertWhy was done in Cursor Agent.

## Session 1 — 22–24 Oct 2026 — product skeleton
- **Asked:** Build Track 02 “explain why” as an AML alert investigator (plan → query fragmented sources → evidence chain → verdict), with Zetaris as the only data path and Meterless for AI + H-MEM.
- **Changed:** `alertwhy/` agent, federation, tools, usage meter, FastAPI + `static/index.html`, synthetic bank, pytest suite, docs.
- **Kept:** Evidence engine decides; LLM only narrates behind a citation guard. Offline DuckDB fallback is labelled as not Zetaris.
- **Tests:** `python -m pytest -q`; `python -m alertwhy.cli eval` → 19/19 on planted labels.

## Session 2 — 24 Oct 2026 — sponsor honesty
- **Asked:** Status of live Zetaris and Nightshift; whether the project was ready to submit.
- **Changed:** `python -m alertwhy.cli status` fails unless Zetaris REST SELECT 1 and Nightshift `/v1/models` are actually online. UI sponsor pills distinguish live vs fallback.
- **Rejected:** Treating DuckDB or a local JSONL ledger as a live sponsor connection.
- **Tests:** `tests/test_cli.py` (status fails offline, passes only when both layers are online).

## Session 3 — 24 Oct 2026 — submission readiness and packaging
- **Asked:** Locate the project, inspect submission blockers, fix judge-facing UI issues, and prepare the submission package.
- **Changed:** Cursor's shared summary reports the queue badge/layout fix, Zetaris setup instructions, `.env` created from the example with credentials left for the user, and a submission zip that excludes `.env`. It also reports `docs/LOCAL_SETUP.md` and `docs/slides.html` as available deliverables.
- **Could not complete:** The shared Cursor output says Docker was unavailable and Zetaris (port 8888) and Nightshift (port 8088) were offline. The user must configure credentials locally, bring up the sponsor services, and verify `python -m alertwhy.cli status` before claiming live integrations.
- **Validation reported by Cursor:** 114 tests passed; `python -m alertwhy.cli eval` returned 19/19 on planted labels. This is a synthetic pipeline check, not real-world accuracy.

## Session 4 — 24 Oct 2026 — setup command and UI regression coverage
- **Asked:** Align the Zetaris setup instructions and UI with the supported `--apply` command and `all` alias, and keep queue verdicts visually separate from alert IDs.
- **Changed:** `scripts/zetaris_setup.py` accepts `all` as an alias for `--apply`; README and the UI banner document both forms. Queue entries render the verdict badge separately. Added regression checks in `tests/test_ui.py`.
- **Validation:** The Cursor excerpt supplied for this entry shows the regression tests being added, but does not show a test run or result.

## Evidence to attach
- This file
- Cursor chat showing the status FAIL lines (no secrets)
- After live connect: a screenshot of the UI with Zetaris live, Meterless online, and at least one AI call on an investigation
