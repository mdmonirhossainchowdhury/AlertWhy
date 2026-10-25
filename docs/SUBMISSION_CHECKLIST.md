# Submission checklist (Open Agent Hackathon 2026)
Deadlines (UTC): registration closes Oct 22 23:59 | build Oct 22 00:00 - Oct 27 23:45 | results Nov 6.
(Dhaka = UTC+6: submissions close Oct 28 05:45.)

- [ ] Registered (one account per person). Team locked on first submission.
- [x] Track selected: Track 02 - The Agent That Can Explain Why.
- [ ] Zetaris live (`python -m alertwhy.cli status` PASS for REST SELECT 1). Adapter + fakes are done; live instance is not running here.
- [ ] Meterless Nightshift live (status PASS for `/v1/models`, investigation shows AI calls > 0). H-MEM is already online.
- [x] Cursor used for development; `docs/CURSOR_LOG.md` completed.
- [ ] Optional: Nemotron configured for narration (in-kind prize "Best use of NVIDIA Nemotron").
- [ ] Declare pre-existing code/components in the submission form (rule 4.2/5.4); H-MEM is vendored Apache-2.0 (`engines/hmem`).
- [ ] GitHub repo pushed with README. Never commit `.env`.
- [ ] Demo video 1-4 min (see docs/DEMO_SCRIPT.md) recorded **after** live sponsor pills.
- [ ] Slide deck exported from `docs/slides.html` / `docs/SLIDES.md`.
- [x] Written explanation: `docs/SPONSOR_TECH.md`.

## Local integration setup

See `docs/LOCAL_SETUP.md`. Copy `.env.example` to `.env` if you do not already have one; never commit `.env` or share its values in chat.

- Zetaris: set `ZETARIS_REST_URL`, `ZETARIS_ORG_ID`, and `ZETARIS_API_KEY`. For the sample Postgres datasources,
  also set `ALERTWHY_PG_PASSWORD`, run `python -m alertwhy.cli export-pg`, `docker compose up -d postgres`,
  `python scripts/zetaris_setup.py` to preview, then `python scripts/zetaris_setup.py --apply`.
- Meterless Nightshift: set `METERLESS_BASE_URL` and `METERLESS_RUNTIME_TOKEN` from the Nightshift Console.
- Restart the app and run `python -m alertwhy.cli status`. All required checks must pass; local DuckDB is only a fallback.
