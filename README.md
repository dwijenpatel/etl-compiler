# etl-solved

**ETL fails in the same ways every time. Those ways are enumerable. Enumerated problems are solvable.**

etl-solved generates correct, conformant data-transformation code by walking a **failure-mode taxonomy** instead of improvising: weird characters (unicode, escapes, BOMs, mojibake), nulls and their many disguises (empty strings, whitespace, `N/A`, `9999`), ambiguous dates, silent truncation, and error handling that actually reports — per-record, per-error-type, and per-run.

## The problem with how ETL gets built today

Bad data is the norm, not the exception: **~26% of open-government CSVs fail a naive parse**, **~20% of CSVs on GitHub use a non-standard dialect**, and a single tool silently autocorrupting gene names left errors in **~30% of scanned genomics papers**. Yet the tools that ingest this data each fail *differently*, and mostly *silently* — verified by direct testing of 15 shipping tools (methodology, sources, and grading in the [taxonomy validation report](docs/taxonomy-validation-report.md)):

- **Dataframe parsers silently coerce.** Put one bad value in a numeric column and pandas turns the whole column to strings; DuckDB and pyarrow turn a 20-digit ID into `1e+32` — precision gone, no error, no warning. The corruption ships downstream wearing a clean face.
- **Warehouse loaders abort the whole batch.** Snowflake (`ABORT_STATEMENT`) and BigQuery (`maxBadRecords=0`) default to failing the entire load on the first bad row — one dirty cell in ten million rows stops the pipeline.
- **Modern ELT hides the damage.** Airbyte and Fivetran keep the row and null the bad field, so it "loads fine" — the failure is invisible until someone downstream trusts a NULL that was actually a parse error. (Airbyte's protocol *cannot even represent* quarantining a bad row.)
- **One-shot LLM codegen improvises.** Ask any chat model for "a script to convert this file" and you get plausible handling — different every time, with no coherent error-reporting architecture and no record of which edge cases it decided to ignore.

**None of these preserves the bad row, tells you what happened, and keeps going.**

## What etl-solved does instead

- **Surfaces every decision, corrupts nothing.** Bad rows are **quarantined with their raw form preserved** (reprocessable), never silently coerced, dropped, or padded. Every auto-fix is **counted**, never silent.
- **Never guesses meaning-changing ambiguity.** MDY-vs-DMY dates, decimal locale, `N/A`-vs-`9999` sentinels — the taxonomy's `ask` class *always* asks; in unattended mode it quarantines rather than reinterprets. (A wrong BOM strip is annoying; a wrong date format silently corrupts every row.)
- **Reports at three granularities, with stable codes.** Per-row error records, per-error-type aggregates, and a run summary + manifest — every error code is a **taxonomy ID** (`TYP-03`, `STR-02`), so a quarantined row traces straight to the documented decision that governs it. Aggregate them across pipelines; they don't drift.
- **Same spec → same behavior.** Decisions live in an auditable `.etlspec.yaml`, and edge-case semantics live in one shared, tested runtime — so regeneration is deterministic and every pipeline handles nulls/encodings/dates *identically*. One-shot generation cannot offer this; consistency is the whole point.

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

`skill/etl-generator/` is an **Agent Skill** conforming to the [Agent Skills open standard](https://agentskills.io) — it runs in **any agent harness that supports the standard** (Claude Code, Claude, Cursor, Gemini CLI, Goose, Codex, OpenCode, and [many more](https://agentskills.io/clients)), not just one product. It's plain Python (standard library only) and hardcodes no client-specific tools; the `SKILL.md` frontmatter declares its requirements via the standard's `compatibility` field.

Drop the `skill/etl-generator/` folder into your harness's skills directory (or zip it), then ask the agent to transform any tabular file — it will profile, interview you only about real detected issues, write the spec, generate the pipeline, and run it to prove it works. It also regenerates pipelines from an existing `.etlspec.yaml` without re-interviewing.

## Repo guide

| Path | What |
|---|---|
| `CLAUDE.md` | Session handoff: decisions, conventions, state, next steps — **start here if you're Claude** |
| `docs/taxonomy.md` | The founding artifact: ~40 failure modes, decision spaces, defaults (v0.2) |
| `docs/taxonomy-validation-report.md` | Validation against a real 244-file corpus + 15-tool evidence census; gaps found/fixed; the ERR-01 default verdict |
| `docs/prd.md` | Original PRD (historical framing; scope + backlog still relevant) |
| `docs/brainstorm-log.md` | Why the project is shaped this way |
| `docs/eval-report-iteration-1.md` · `-2.md` | Benchmarks vs. baseline; why single-run assertions don't separate them and what to measure next |
| `skill/etl-generator/` | The skill (Agent Skills standard): workflow, references, profiler, runtime |
| `corpus/` | Reproducible messy-data corpus + profiler audit harness (taxonomy validation) |
| `tests/` | Runtime + profiler unit suite (`python3 -m unittest discover -s tests`) |
| `examples/messy-sample/` | End-to-end runnable demo |
| `evals/` | Eval inputs + iteration-1 and iteration-2 results |

## Status

**v0.2** — taxonomy, profiler, and runtime hardened and validated against reality. The runtime has a real unit-test suite (55 tests); the taxonomy was validated against a 244-file corpus of genuinely messy real-world data plus a graded, primary-sourced census of 15 shipping ETL tools (the competitive claims above come from it). Six profiler detection bugs found on real portal files were fixed; a new failure mode (magnitude-suffixed numerics) was added end-to-end. The one load-bearing default question — quarantine-vs-fail-loud — was settled by a dedicated adversarial evidence pass (verdict: keep quarantine; see the validation report).

Eval iteration 2 (contract-level assertions, isolated baselines) found that a strong model satisfies the *contract* unaided on single runs — so the skill's real value is determinism, stable taxonomy codes, and a tested shared runtime, which single-run scoring can't capture. **Next:** a determinism-focused eval and a deterministic `spec → pipeline.py` compiler so regeneration needs no model in the loop.
