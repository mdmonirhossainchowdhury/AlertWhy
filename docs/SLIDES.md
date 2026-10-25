# Slide deck outline (paste into Slides/PowerPoint/Canva)
1. **Title** - AlertWhy: AML alerts, explained. Track 02. Team + one-line promise.
2. **Problem & users** - alert overload, ~25 min/alert manual (state as an assumption), regulators demand a reason. Users: AML analysts, MLROs.
3. **Solution** - screenshot: verdict + evidence chain + cost meter.
4. **Agent workflow** - plan -> 10 tools -> evidence chain -> decide -> explain -> audit (use the diagram in README).
5. **Architecture** - 5 fragmented sources -> Zetaris -> agent -> Meterless.
6. **Tech stack** - Python, FastAPI, DuckDB, vanilla JS UI, pytest, optional Nemotron via OpenAI-compatible API, built with Cursor.
7. **How we used Zetaris** - query-in-place across DB/Parquet/CSV/JSONL; one interface, same SQL; no data copy; governance by design.
8. **How we used Meterless** - one usage event per investigation (queries, rows, tokens, $), per-case budget guardrail, ROI view.
9. **What is unique** - evidence engine decides, LLM only narrates behind a citation guard; mitigating evidence; says "unsure".
10. **Results (honest)** - 19/19 on planted synthetic scenarios (pipeline validation, not real-world accuracy), 114 tests. Open `docs/slides.html` in a browser to present or export.
11. **NVIDIA** - where Nemotron narrates (only if used).
12. **Future** - real FATF data, graph analytics, analyst feedback loop, SAR drafting, bank pilot.
