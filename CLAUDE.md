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

### Session 2026-07-20 (crash-recovered): runtime hardening + taxonomy validation

- **Runtime v0.2.0** (`etl_runtime.py`): trims now counted (TYP-10/NUL-02, ERR-04); `to_bool` casefold acceptances counted; manifest carries `spec_version`/`spec_sha256`/`generator_version` (ERR-06); **STR-05 exact-duplicate detection** (keep-and-report default, `drop_exact` explicit); run-errors return exit 1 *with* reports (ERR-05, was bare traceback); `to_decimal(magnitude=True)` for TYP-12. Backed by `tests/test_etl_runtime.py` (real unit suite — the "tested runtime" premise is now fact, not aspiration).
- **Taxonomy v0.2** (both copies synced): added **TYP-12** (magnitude/scale-suffixed numerics). Validation done via a real corpus, NOT the 5-file plan — see below.
- **Taxonomy validation** (`docs/taxonomy-validation-report.md` — READ THIS before taxonomy work): 3 evidence streams — (A) profiler audit over 244 real files (`corpus/`, reproducible), (B) literature/incidents + (D) 15-tool census, both in the **evidence corpus at `~/repos/etl-evidence`** (evidence-kit format, retrieval-grade). Findings: taxonomy *categories* held up (no entry unused, one gap closed as TYP-12, rest deferred as not-yet-finite); the *profiler* was the weak link (6 detection bugs found + fixed + tested: empty-file crash, TYP-06 over-fire, STR-06 preamble, TYP-03 dotted dates, NUL-03 sentinels, ENC-02 doubled-BOM).
- **Open default question**: no surveyed tool defaults to row-quarantine (ERR-01). Flagged for a dedicated adversarial-grade pass before any change — do NOT change ERR-01 on current (retrieval-grade) evidence.
- **Eval iteration 2**: 6 runs executed + archived under `evals/iteration-2/` but **NOT graded** — they predate the v0.2 profiler/runtime changes. Regenerate, don't grade as-is. Also: baseline arm needs strong isolation (a baseline subagent copied the skill runtime verbatim until told not to read the repo).

## Prioritized next steps

1. **Adversarial-grade evidence pass on ERR-01** (quarantine default) — the one load-bearing default question the validation raised. Run in `~/repos/etl-evidence` (evidence-kit Operation 2, adversarial); only then is a default change defensible.
2. **Interview batching** (SKILL.md + profiler): homogeneous per-column `ask` findings must be grouped into one dataset-level question (corpus showed 9k TYP-06 questions on a wide file). Generalizes the NUL-01 batching note.
3. **Rerun eval iteration 2** against the v0.2 profiler/runtime (archived runs predate it) with the contract-level assertions in `evals/iteration-2/*/eval_metadata.json`; strong baseline isolation (see note above).
4. **Deterministic compiler**: today codegen is Claude following `references/codegen-guide.md`. Build `spec → pipeline.py` as an actual script (template/AST-based) so regeneration is byte-deterministic without a model in the loop. This was always the intended end-state.
5. **Charset detection** for ENC-01: the Latin-1 fallback always "succeeds", so real Shift-JIS (JP corpus files) is mislabeled `latin-1` with mojibake headers. Needs byte-n-gram/chardet-style detection within the stdlib-only constraint.
6. **Extract the runtime into a proper package** (`pip install etl-solved-runtime`) — the unit-test suite now exists (`tests/`), so packaging is the remaining step.
7. **Deterministic compiler** (see #4 above — now the intended end-state after the taxonomy stabilizes).
8. **TypeScript runtime + target**; **SQL/dbt target** (analyst-serving surface). Sequenced after the Python path is solid.

Runtime/profiler test suite lives in `tests/` (`python3 -m unittest discover -s tests`) — 55 tests, keep it green. Corpus audit: `python3 corpus/audit.py` after profiler changes.

## Things to NOT do

- Don't add hosted execution, connectors, nested-data support, or multi-table joins yet (PRD non-goals still stand; spec format was designed so joins/nesting can arrive without rewrites — namespaced, path-like source refs).
- Don't let generated pipelines grow inline edge-case logic. If the runtime lacks something, extend the runtime.
- Don't change taxonomy defaults casually — that's a major-version event affecting every spec that relied on them.
