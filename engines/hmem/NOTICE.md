# Attribution

`src/`, `conformance/`, `LICENSE` and `SPEC_AGENTS.md` are copied **unmodified** from the Meterless open-source
repository (https://github.com/Meterless/Meterless, `engines/hmem/`, Apache License 2.0).
That is the H-MEM engine: tiered memory, hybrid retrieval, feedback deltas, trust ledger, sleep cycle, conflict gate.

AlertWhy-specific code in this folder: `bridge.ts`, `package.json`, `tsconfig.json`, and this file.

Run the engine's conformance suite: `npm install && npm run conformance` (expected: 13/13 checks passed).
