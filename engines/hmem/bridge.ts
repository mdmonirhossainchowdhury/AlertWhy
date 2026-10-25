// JSON-lines bridge: lets the Python agent use the Meterless H-MEM engine.
// One request per stdin line: {"id": 1, "op": "add", "args": {...}}
// One response per stdout line: {"id": 1, "ok": true, "result": ...} or {"id": 1, "ok": false, "error": "..."}
// The engine in ./src is the unmodified Meterless reference implementation (Apache-2.0).
import readline from "node:readline";
import { HMEM } from "./src/index.ts";

const persistDir = process.env.HMEM_DIR || undefined;
const hmem = new HMEM({ persistDir, actor: "alertwhy" });

const slim = (m: any) => ({
  id: m.id, content: m.content, tags: m.tags, confidence: m.confidence, source: m.source, layer: m.layer,
  type: m.type, accessCount: m.accessCount, timestamp: m.timestamp, entities: m.entities,
  supersededBy: m.supersededBy ?? null,
});

const ops: Record<string, (a: any) => unknown> = {
  ping: () => ({ engine: "hmem", records: hmem.store.all().length }),
  add: (a) => slim(hmem.add({
    content: a.content, source: a.source, confidence: a.confidence, tags: a.tags,
    type: a.type, layer: a.layer ?? "working", supersedes: a.supersedes,
  })),
  // Mark an older memory as replaced by a newer one (analyst overturned an earlier decision).
  // Uses the engine's own update(), so it is written to the trust ledger; ranking then penalizes it (spec 6.1).
  mark_superseded: (a) => slim(hmem.store.update(a.old, { supersededBy: a.new })),
  query: (a) => {
    const r = hmem.query(a.query, { topN: a.topN ?? 8, threshold: a.threshold ?? 0 });
    return {
      memories: r.memories.map((x: any) => ({ ...slim(x.memory), relevance: x.relevance })),
      trace: r.trace,
    };
  },
  feedback: (a) => { hmem.feedback(a.id, a.kind); return slim(hmem.store.get(a.id)); },
  all: () => hmem.store.all().map(slim),
  ledger: () => (hmem.ledger as any).all(),
  sleep_preview: () => hmem.sleep.preview(),
};

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  if (!line.trim()) return;
  let req: any;
  try { req = JSON.parse(line); } catch { return; }
  try {
    const fn = ops[req.op];
    if (!fn) throw new Error(`unknown op ${req.op}`);
    process.stdout.write(JSON.stringify({ id: req.id, ok: true, result: fn(req.args ?? {}) }) + "\n");
  } catch (e) {
    process.stdout.write(JSON.stringify({ id: req.id, ok: false, error: (e as Error).message }) + "\n");
  }
});
rl.on("close", () => process.exit(0));
