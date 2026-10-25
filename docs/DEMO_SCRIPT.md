# 4-minute demo script (hard limit 4:00; target 3:40)

**0:00-0:25 Problem.** "Every bank gets thousands of AML alerts, mostly false positives. Analysts open five systems per alert and must justify the decision to regulators. AlertWhy investigates for them and shows its evidence."

**0:25-0:50 Architecture (one slide).** Five fragmented sources -> Zetaris query-in-place -> agent -> verdict -> Meterless usage event. "No data is copied; the same SQL runs on Zetaris."

**0:50-1:50 Case 1: sanctions false positive (A-1003).** Click Investigate. Point at: name matches a watchlist entry, but E-item shows DOB and country differ; history shows 12 months of identical payments. Verdict CLOSE, cited. "A human would spend 25 minutes on this."

**1:50-2:50 Case 2: layering (A-1004).** New entity, wires in and straight out within hours to high-risk jurisdictions, a counterparty on the watchlist. Show the evidence chain, risk 100, ESCALATE, next steps. Open one audit-trail step: "every query is logged for the regulator."

**2:50-3:15 Honesty.** Open A-1009: mixed signals -> REVIEW, confidence 50%. "It says when it is unsure."

**3:15-3:40 Cost + sponsors.** Show the usage meter: queries, tokens, cost per alert vs manual. Click *Run all & evaluate*. State clearly it is a synthetic benchmark. Mention Zetaris, Meterless, Nemotron narration (only if configured), and that it was built in Cursor.

Rehearse once with the real sponsor integrations on; if any adapter is not live, say "local federation / local ledger" - do not claim otherwise.
