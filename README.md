# AlertWhy

An AML alert investigator that explains why it recommends **ESCALATE**, **CLOSE**, or **REVIEW**. AlertWhy queries fragmented data sources, builds a numbered evidence chain with supporting and mitigating facts, and records an audit trail.

## Source code

The full project source is available in [`alertwhy-submission.zip`](./alertwhy-submission.zip). The archive excludes local secrets (`.env`), generated data, caches, and `node_modules`. Extract the archive to work with the project files.

## Quick start

Requirements: Python 3.10+.

```bash
# Windows
run.bat

# Or install requirements and run the CLI
python -m pip install -r requirements.txt
python -m alertwhy.cli eval
python -m pytest -q
```

Open http://localhost:8000 after starting the app.

## Verification

Offline checks completed locally: **114 tests passed** and the synthetic-data evaluation passed **19/19** scenarios. These are offline/demo results, not real-world accuracy measures.

**Live sponsor connectivity is not verified.** Do not claim live Zetaris or Meterless/Nightshift integration unless `python -m alertwhy.cli status` passes for both integrations and the UI shows both sponsors connected. Follow [`docs/LOCAL_SETUP.md`](docs/LOCAL_SETUP.md) in the source archive for setup guidance.

## Data and limitations

The included demo uses synthetic AML data. Country-risk tiers are illustrative. This is decision-support software; a human analyst must review outcomes. Do not use it for real financial decisions without independent validation.

## License

The project declares MIT for the hackathon submission. Vendored H-MEM components retain their Apache-2.0 license and notices in the source archive.
