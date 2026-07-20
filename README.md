# etl-solved

**ETL fails in the same ways every time. Those ways are enumerable. Enumerated problems are solvable.**

etl-solved generates correct, conformant data-transformation code by walking a **failure-mode taxonomy** instead of improvising: weird characters (unicode, escapes, BOMs, mojibake), nulls and their many disguises (empty strings, whitespace, `N/A`, `9999`), ambiguous dates, silent truncation, and error handling that actually reports — per-record, per-error-type, and per-run.

## How it works

```
sample input ──▶ PROFILER ──▶ findings (taxonomy IDs + evidence)
                                 │
                                 ▼
                        INTERVIEW (only about what was detected;
                         meaning-changing ambiguity is never guessed)
                                 │
                                 ▼
                        SPEC (.etlspec.yaml — every decision, with provenance)
                                 │
                                 ▼
                        CODEGEN ──▶ thin pipeline.py  +  etl_runtime.py (shared, tested)
                                 │
                                 ▼
                        VERIFIED RUN ──▶ output.csv + quarantine.csv
                                          + errors.jsonl + summary.json + manifest.json
```

Three properties make the output trustworthy: the **spec** is a complete, auditable record of every edge-case decision (including applied defaults); the **runtime** implements each taxonomy entry exactly once, so every pipeline handles nulls/encodings/dates identically; and **error codes are taxonomy IDs**, so a quarantined row marked `TYP-03` traces straight to the documented decision that governs it.

## Try it in 30 seconds

```bash
# See the profiler detect 17 planted failure modes:
python3 skill/etl-generator/scripts/profile.py examples/messy-sample/messy_sample.csv

# Run a generated-style pipeline over the same filthy file:
python3 examples/messy-sample/smoke_pipeline.py
# → exit 2 (completed with quarantined rows); inspect examples/messy-sample/etl_out/
```

Everything is stdlib-only Python. No dependencies.

## Using the skill

`skill/etl-generator/` is a Claude Code skill. Zip it (or use skill-creator's `package_skill`) and install, then ask Claude to transform any tabular file — it will profile, interview you only about real detected issues, write the spec, generate the pipeline, and run it to prove it works. It also regenerates pipelines from existing `.etlspec.yaml` files without re-interviewing.

## Repo guide

| Path | What |
|---|---|
| `CLAUDE.md` | Session handoff: decisions, conventions, state, next steps — **start here if you're Claude** |
| `docs/taxonomy.md` | The founding artifact: ~40 failure modes, decision spaces, defaults |
| `docs/prd.md` | Original PRD (historical framing; scope + backlog still relevant) |
| `docs/brainstorm-log.md` | Why the project is shaped this way |
| `docs/eval-report-iteration-1.md` | Benchmark vs. baseline Claude, analyst findings, iteration-2 plan |
| `skill/etl-generator/` | The skill: workflow, references, profiler, runtime |
| `examples/messy-sample/` | End-to-end runnable demo |
| `evals/` | Eval inputs and full iteration-1 results (open `evals/iteration-1/review.html`) |

## Status

v0.1 — foundations built and verified in the founding session (2026-07-17): taxonomy drafted, profiler and runtime working, skill scaffolded and packaged, first eval iteration run (with-skill 20/20 assertions vs. baseline 19/20 — see the eval report for why the qualitative gap is bigger than the number). Next: contract-level eval assertions, taxonomy validation against real-world files, deterministic spec→code compiler, runtime extracted as a tested package.
