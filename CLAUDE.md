# CLAUDE.md — etl-solved

Context for Claude sessions working in this repo. Read this first; it is the handoff from the founding session (July 17–18, 2026, Cowork).

## What this project is

**etl-solved** generates correct, conformant ETL code by making every edge-case decision explicit, finite, and recorded. The observation that founded it: ETL fails in the *same recurring ways* (nulls/sentinels, encoding damage, ambiguous dates, silent truncation, inconsistent error reporting), each failure mode has a small finite decision space, and therefore the whole class is generically solvable — by walking a taxonomy instead of improvising.

**This is not a commercial product.** The explicit goal is that the workflow *exists* and is correct. No revenue, no moat concerns. Open-sourcing is consistent with the goal.

## The four pillars (architecture)

1. **The taxonomy** (`docs/taxonomy.md`) — ~40 failure modes with stable IDs (ENC/STR/NUL/TYP/KEY/ERR), each with detection signals, a finite decision space, a house default, and a class (`fix` / `ask` / `row-error` / `run-error`). This is the intellectual core; everything else compiles from it. Error codes in generated pipelines ARE taxonomy IDs.
2. **The spec** (`.etlspec.yaml`, format in `skill/etl-generator/references/spec-format.md`) — every mapping and every policy decision, each with provenance (`explicit` / `detected-confirmed` / `default` / `unconfirmed`). The spec, not any conversation, is the source of truth. Same spec → same behavior.
3. **The runtime** (`skill/etl-generator/assets/etl_runtime.py`) — the ONLY place edge-case semantics are implemented. Stdlib-only. Generated pipelines are thin orchestration that import it. This was an explicit decision ("Option Two"): fixes propagate, the test surface accumulates in one place. The rejected alternative (inlining hardened code per pipeline) may return later as an optional zero-dependency export mode — much later.
4. **The skill** (`skill/etl-generator/`) — a Claude Code skill implementing the workflow: profile → detection-driven interview → mapping proposal → spec → codegen → verified run. Skill-first distribution was deliberate (meets engineers where their data already is; the FlashFill lesson: distribution beats features).

## Non-negotiable conventions

- **Never guess meaning-changing ambiguity.** `ask`-class findings (TYP-03 date ambiguity, TYP-02 decimal locale, NUL-03 sentinels, TYP-04 timezones) always require confirmation. In unattended contexts: choose the option that quarantines rather than reinterprets, mark `provenance: unconfirmed`, list in the spec's `review_required`.
- **Every decision lands in the spec, including defaults.** Silence is a bug.
- **Auto-fixes are counted** (ERR-04). Warnings never quarantine; they are always tallied per column per taxonomy ID.
- **Reports at all three granularities, always** (ERR-03): per-row error records, per-error-type aggregates, run summary + manifest.
- **New edge-case behavior goes in the runtime**, never inline in a pipeline, with taxonomy IDs in comments.
- **Taxonomy versioning:** adding a failure mode = minor version; changing a default = major version.
- **Duplicated file:** `skill/etl-generator/references/taxonomy.md` is a copy of `docs/taxonomy.md` (canonical). If you edit one, sync the other.

## Repo map

```
CLAUDE.md                  ← you are here
README.md                  ← human-facing overview
docs/
  taxonomy.md              ← THE founding artifact (canonical copy)
  prd.md                   ← original product PRD (historical note at top; still useful for scope + P1/P2 backlog)
  brainstorm-log.md        ← how the strategy evolved; decisions and their rationale
  eval-report-iteration-1.md ← eval results, analyst findings, iteration-2 plan
skill/etl-generator/       ← the Claude Code skill (self-contained)
  SKILL.md                 ← workflow instructions
  references/              ← taxonomy copy, spec-format.md, codegen-guide.md
  scripts/profile.py       ← working profiler (stdlib-only); emits findings keyed by taxonomy IDs
  assets/etl_runtime.py    ← the shared runtime (stdlib-only), v0.1.0
  evals/evals.json         ← eval prompts + expectations
examples/messy-sample/     ← runnable end-to-end demo (see its README)
evals/
  inputs/                  ← eval input files (messy CSVs, a spec, samples)
  iteration-1/             ← full eval results: outputs per run, grading.json, benchmark.json/md, review.html
```

## Current state (verified working)

- Profiler detects all 17 planted failure modes in `examples/messy-sample/messy_sample.csv` (ENC-02/03/04/05/06, STR-02/04/05/06/07, NUL-01/02/03, TYP-01/03/06/07).
- Runtime + generated-style pipeline runs end-to-end: quarantines the ragged row with a coded record, counts every auto-fix, excludes footer via explicit `SkipRow`, atomic output, manifest. `python3 examples/messy-sample/smoke_pipeline.py` → exit 2 (completed with quarantine).
- Eval iteration 1 (3 tasks × with-skill/baseline, subagent runs, graded): **with-skill 20/20 assertions, baseline 19/20.** Full analysis in `docs/eval-report-iteration-1.md`. Key insight: the model writes good ETL unaided, so the skill's value is contract-level (machine-readable accounting completeness, quarantine-not-pad, keep-duplicates-by-default, repair-as-opt-in), which iteration-1 assertions only partially priced in.
- Skill packaging: the skill dir zips into a `.skill` (in Claude Code: `python -m scripts.package_skill` from the skill-creator skill, or just zip the folder).

## Prioritized next steps

1. **Eval iteration 2** with discriminating, contract-level assertions (drafted in `docs/eval-report-iteration-1.md`): machine-readable accounting must cover EVERY modified/dropped/padded row; duplicate rows kept-and-reported by default; quarantine preserves raw rows for reprocessing; mojibake repair is opt-in. Rerun with-skill + baseline, compare to iteration 1.
2. **Taxonomy validation** against 5 genuinely messy real-world files (plan is in `docs/taxonomy.md` § Validation Plan). Every observed problem should map to an ID; unmapped problems become new entries.
3. **Deterministic compiler**: today codegen is Claude following `references/codegen-guide.md`. Build `spec → pipeline.py` as an actual script (template/AST-based) so regeneration is byte-deterministic without a model in the loop. This was always the intended end-state.
4. **Extract the runtime into a proper package** (`pip install etl-solved-runtime` or similar) with a real unit-test suite — today it's copied per-project from skill assets, tested only via the smoke example and evals.
5. **Profiler refinements**: batch NUL-01 into one dataset-level question (currently asks per column); exclude suspected footer rows from per-column stats (footer empties currently inflate NUL-01 counts); TYP-06 single-value column edge cases.
6. **Skill description trigger optimization** (skill-creator's `run_loop.py`, available in Claude Code) — not yet run.
7. **TypeScript runtime + target** (PRD P0 originally; now sequenced after the Python path is solid).
8. **SQL/dbt target** — the analyst-serving surface (see brainstorm-log: analysts author specs; SQL is their runtime).

## Things to NOT do

- Don't add hosted execution, connectors, nested-data support, or multi-table joins yet (PRD non-goals still stand; spec format was designed so joins/nesting can arrive without rewrites — namespaced, path-like source refs).
- Don't let generated pipelines grow inline edge-case logic. If the runtime lacks something, extend the runtime.
- Don't change taxonomy defaults casually — that's a major-version event affecting every spec that relied on them.
