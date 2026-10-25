# What is already done vs what only you can do

The agent, UI, tests, H-MEM memory engine, Zetaris adapter, Nightshift adapter, demo script, slide outline, and sponsor write-up are in this repo. **Do not submit until `python -m alertwhy.cli status` prints PASS for Zetaris and Nightshift.** Judges require live Zetaris and Meterless, not the DuckDB fallback.

This machine currently has:

- no Docker (`docker` is not installed)
- nothing listening on port 8888 (Zetaris REST)
- nothing listening on port 8088 (Nightshift)
- a local `.env` with a Postgres password and placeholder URLs, **without** `ZETARIS_API_KEY` or `METERLESS_RUNTIME_TOKEN`

## 1. Sample database (5 minutes)

Already generated: `python -m alertwhy.cli generate` and `python -m alertwhy.cli export-pg`.

Install Docker Desktop, then from this folder:

```bat
docker compose up -d postgres
```

`.env` already has `ALERTWHY_PG_PASSWORD`. Do not paste it into chat.

## 2. Zetaris (Participant Setup Guide on HackOS)

1. Install and start Zetaris with the **exact** commands in that guide. REST should listen on port 8888.
2. Open the Zetaris web UI yourself and create an API key. Do not ask an AI to create or fetch it.
3. Put the REST URL and key in `.env` as `ZETARIS_REST_URL` and `ZETARIS_API_KEY`. Local default URL is `http://localhost:8888/api/v1.0`.
4. Preview, then apply:

```bat
python scripts/zetaris_setup.py
python scripts/zetaris_setup.py --apply
```

`python scripts/zetaris_setup.py all` is the same as `--apply`.

## 3. Meterless Nightshift (Participant Setup Guide on HackOS)

1. Install and start the Nightshift runtime. It should listen on port 8088.
2. Copy the runtime token from the Nightshift Console into `.env` as `METERLESS_RUNTIME_TOKEN`.
3. `METERLESS_BASE_URL` should be `http://127.0.0.1:8088/v1`.
4. You can also paste the connect link in the UI (**Connect Meterless**) without writing the token to disk.

## 4. Verify (required before recording or submitting)

```bat
python -m alertwhy.cli status
python -m alertwhy.cli investigate A-1001
python -m alertwhy.cli eval
python -m pytest -q
```

Status must PASS Zetaris REST SELECT 1, Nightshift models, and H-MEM. The UI sponsor pills must show Zetaris live and Meterless online, and an investigation must show at least one AI call.

If status still FAILs, paste the status output (it has no secrets).

## 5. Submission materials you still produce locally

| Item | File / action |
|---|---|
| Demo video 1–4 min | Follow `docs/DEMO_SCRIPT.md` with **live** sponsor pills |
| Slide deck | Outline in `docs/SLIDES.md`; open `docs/slides.html` in a browser and export PDF/screenshots |
| GitHub | Create a repo, push this project, clone fresh, run the README. Never commit `.env` |
| HackOS form | Track 02. Attach video, deck, repo URL. Paste `docs/SPONSOR_TECH.md`. Declare any pre-existing components |
| Cursor evidence | `docs/CURSOR_LOG.md` plus a screenshot of a Cursor session |

Deadline: **27 October 2026, 23:45 UTC**.
