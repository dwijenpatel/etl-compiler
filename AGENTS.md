# AGENTS.md — etl-compiler

Context for AI agents working in this repo — any harness (this file follows the AGENTS.md convention; `CLAUDE.md` is a pointer here). Read this first; it is the living handoff, begun at the founding session (July 17–18, 2026).

## What this project is

**etl-compiler** generates correct, conformant ETL code by making every edge-case decision explicit, finite, and recorded. The observation that founded it: ETL fails in the *same recurring ways* (nulls/sentinels, encoding damage, ambiguous dates, silent truncation, inconsistent error reporting), each failure mode has a small finite decision space, and therefore the whole class is generically solvable — by walking a taxonomy instead of improvising.

**This is not a commercial product.** The explicit goal is that the workflow *exists* and is correct. No revenue, no moat concerns. Open-sourcing is consistent with the goal.

## The four pillars (architecture)

1. **The taxonomy** (`docs/taxonomy.md`) — ~40 failure modes with stable IDs (ENC/STR/NUL/TYP/KEY/ERR), each with detection signals, a finite decision space, a house default, and a class (`fix` / `ask` / `row-error` / `run-error`). This is the intellectual core; everything else compiles from it. Error codes in generated pipelines ARE taxonomy IDs.
2. **The spec** (`.etlspec.yaml`, format in `skill/etl-generator/references/spec-format.md`) — every mapping and every policy decision, each with provenance (`explicit` / `detected-confirmed` / `default` / `unconfirmed`). The spec, not any conversation, is the source of truth. Same spec → same behavior.
3. **The runtime** (`skill/etl-generator/assets/` — TWO files, split on the effect boundary: `etl_coercers.py` the deterministic core — no I/O/clock, only effect is counting into the passed `RunReport`; `etl_runtime.py` the I/O driver, re-exporting the core so pipelines import only `etl_runtime`) — the ONLY place edge-case semantics are implemented. Stdlib-only, **mypy-strict typed** (`TypedDict` contracts for config/records, `CellValue` union; the compiler type-checks against `PipelineConfig` via a TYPE_CHECKING import). Copy BOTH files beside a generated pipeline. This was an explicit decision ("Option Two"): fixes propagate, the test surface accumulates in one place. The agreed purity bar (2026-07-21): deterministic, effect-confined-to-a-passed-accumulator — not literal purity.
4. **The skill** (`skill/etl-generator/`) — an Agent Skill conforming to the [Agent Skills open standard](https://agentskills.io), runnable by any supporting harness. It implements the workflow: profile → detection-driven interview → mapping proposal → spec → codegen → verified run. Skill-first distribution was deliberate (meets engineers where their data already is; the FlashFill lesson: distribution beats features). Keep it harness-agnostic: no client-specific tool names or paths in the skill.

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
AGENTS.md                  ← you are here (canonical agent instructions)
CLAUDE.md                  ← pointer that imports this file (Claude Code convention)
README.md                  ← human-facing overview
docs/
  taxonomy.md              ← THE founding artifact (canonical copy), v0.4
  taxonomy-validation-report.md ← [local-only] 3-stream validation findings + ERR-01 verdict
  prd.md                   ← [local-only] original product PRD
  brainstorm-log.md        ← [local-only] how the strategy evolved; decisions + rationale
  eval-report-iteration-1.md / -2.md ← [local-only] eval analysis and what to measure next
skill/etl-generator/       ← the Agent Skill (open standard; self-contained)
  SKILL.md                 ← workflow instructions
  references/              ← taxonomy copy, spec-format.md, codegen-guide.md
  scripts/profile.py       ← working profiler (stdlib-only); emits findings keyed by taxonomy IDs
  scripts/compile_spec.py  ← deterministic spec → pipeline.py compiler (stdlib-only, fail-loud), v0.5.0
  assets/etl_coercers.py   ← runtime core (deterministic, effect-confined; mypy-strict)
  assets/etl_runtime.py    ← runtime I/O driver (re-exports the core), v0.7.0
  evals/evals.json         ← eval prompts + expectations (relative paths)
corpus/                    ← [local-only] reproducible messy-data corpus + audit harness
tests/                     ← runtime + profiler + compiler unit suite (keep green)
examples/messy-sample/     ← [local-only] runnable end-to-end demo
evals/
  inputs/                  ← eval input files (messy CSVs, a spec, samples) — also test fixtures
  iteration-4/             ← current eval results (iteration-1/ through -3/ are [local-only])
```

**[local-only]** = untracked development artifacts (see `.gitignore`): present on the
maintainer's machine, absent from a fresh clone — a customer clone is just the skill +
taxonomy + tests + current eval. Do not add tracked links pointing at local-only paths.
The external evidence corpus backing taxonomy/default decisions is likewise local:
`~/repos/etl-evidence` (see the memory/handoff notes).

## Current state (verified working)

- Profiler detects all 17 planted failure modes in `examples/messy-sample/messy_sample.csv` (ENC-02/03/04/05/06, STR-02/04/05/06/07, NUL-01/02/03, TYP-01/03/06/07).
- Runtime + generated-style pipeline runs end-to-end: quarantines the ragged row with a coded record, counts every auto-fix, excludes footer via explicit `SkipRow`, atomic output, manifest. `python3 examples/messy-sample/smoke_pipeline.py` → exit 2 (completed with quarantine).
- Eval iteration 1 (3 tasks × with-skill/baseline, subagent runs, graded): **with-skill 20/20 assertions, baseline 19/20.** Full analysis in `docs/eval-report-iteration-1.md`. Key insight: the model writes good ETL unaided, so the skill's value is contract-level (machine-readable accounting completeness, quarantine-not-pad, keep-duplicates-by-default, repair-as-opt-in), which iteration-1 assertions only partially priced in.
- Skill packaging: zip the `skill/etl-generator/` folder — no harness-specific tooling required; any Agent-Skills-compatible harness consumes the directory (or the zip) directly.

### Session 2026-07-20 (crash-recovered): runtime hardening + taxonomy validation

- **Runtime v0.2.0** (`etl_runtime.py`): trims now counted (TYP-10/NUL-02, ERR-04); `to_bool` casefold acceptances counted; manifest carries `spec_version`/`spec_sha256`/`generator_version` (ERR-06); **STR-05 exact-duplicate detection** (keep-and-report default, `drop_exact` explicit); run-errors return exit 1 *with* reports (ERR-05, was bare traceback); `to_decimal(magnitude=True)` for TYP-12. Backed by `tests/test_etl_runtime.py` (real unit suite — the "tested runtime" premise is now fact, not aspiration).
- **Taxonomy v0.2** (both copies synced): added **TYP-12** (magnitude/scale-suffixed numerics). Validation done via a real corpus, NOT the 5-file plan — see below.
- **Taxonomy validation** (`docs/taxonomy-validation-report.md`, [local-only] — READ THIS before taxonomy work when present): 3 evidence streams — (A) profiler audit over 244 real files (`corpus/`, reproducible), (B) literature/incidents + (D) 15-tool census, both in the **evidence corpus at `~/repos/etl-evidence`** (evidence-kit format, retrieval-grade). Findings: taxonomy *categories* held up (no entry unused, one gap closed as TYP-12, rest deferred as not-yet-finite); the *profiler* was the weak link (6 detection bugs found + fixed + tested: empty-file crash, TYP-06 over-fire, STR-06 preamble, TYP-03 dotted dates, NUL-03 sentinels, ENC-02 doubled-BOM).
- **ERR-01 default RESOLVED (adversarial pass, 2026-07-20): keep quarantine.** Corpus pass 2 (`~/repos/etl-evidence/external/error-disposition-defaults/`, distilled). Content-vs-movement crux resolved: fail-loud defaults fire on content too, but tools split into 4 poles (fail-loud / annotate-and-keep / silent-coerce / quarantine) — none quarantine, and the most-used parsers *silently coerce* content errors, so quarantine is strictly better there. Non-default follow-ups the evidence supports (not yet done): promote ERR-01 option (c) "annotate" to first-class with Airbyte's `_airbyte_meta {field,change,reason}` shape; adopt DuckDB `reject_errors` schema for ERR-03(i); consider an ADF-style `redirect` disposition.
- **Interview batching DONE**: profiler emits `interview_groups` (homogeneous per-column ask-findings collapsed — 9k→1 on the wide file); SKILL.md drives the interview from it.
- **Eval iteration 2 DONE** (`evals/iteration-2/`, `docs/eval-report-iteration-2.md`): v0.2 reruns, contract-level assertions, isolated baselines. **skill 19/19 = baseline 19/19** — a strong isolated Opus baseline passes even the hard contract assertions unaided. The skill's real value (determinism, stable taxonomy codes, tested runtime, editable spec) is NOT measurable on single runs. **Iteration 3 must measure determinism/consistency, not single-run correctness** (harness sketched in the report).
- **Deterministic compiler DONE** (`skill/etl-generator/scripts/compile_spec.py`, v0.1.0): `spec → pipeline.py` with no model in the loop; same spec bytes → byte-identical pipeline (header embeds spec sha256; no timestamps). Includes a strict stdlib-only loader for the etlspec subset of YAML — fail-loud (SpecError + line number) on anything outside the documented format, including YAML 1.1 boolean traps (`Y`/`N`/`yes`/`no` stay strings; only `true`/`false` resolve). Validates spec completeness (all 10 policy keys, mapped-or-declared target columns, known ops/kwargs) before emitting. `concat`/`split` ops not yet supported (fail-loud, points to `op: expr`). SKILL.md step 6 is now compiler-first: the agent authors the spec, the compiler owns the code. Tests in `tests/test_compiler.py` (25) incl. byte-determinism and end-to-end behavior vs the vendor sample.
- **Compiler-coverage gap CLOSED (runtime 0.3.0, compiler 0.2.0, spec format 0.2):** upstreamed the agent-written `repair_mojibake` (ENC-06, signature-gated, never lossy, report-counted) and `skip_if` (STR-06) into the runtime; compiler emits `repair_mojibake` + `concat` (NUL-05 sql nulls) as first-class ops, declarative `skip_rows:` rules (regex on raw value → counted SkipRow guards), and passes `report` into `expr` helpers; `split` stays declined (miss-semantics have no taxonomy home — relates to the deferred embedded-values candidate). SKILL.md gained the artifact-naming convention. Proof: `evals/inputs/orders_export.etlspec.yaml` — the 17-trap file fully compiler-expressible, end-to-end test pins exact accounting (9→6+1, ENC-06 ×1, STR-06 ×2, STR-05 ×1, STR-02 ×1).
- **ERR-01 follow-ups DONE (runtime 0.4.0, compiler 0.3.0, spec format 0.3):** `annotate` promoted to a first-class disposition (ERR-01 option c, evidence-backed): content failures NULL the field, ledger `{field, change: NULLED, reason: <taxonomy id>}` to `changes.jsonl` (Airbyte `_airbyte_meta.changes` shape, our vocabulary), aggregate in `summary.annotations_by_type`, row loads, exit 2. STR-02 (structural) and NUL-04 (declared non-nullable) never annotate — they quarantine (the content-vs-movement crux, applied). Unknown disposition = SpecError at compile + RunError at run — no silent fallback. Compiler now emits per-field `FIELD_TRANSFORMS` + `_row_guards` (annotate needs field granularity; quarantine/fail-fast unchanged semantics). `errors.jsonl` records adopt DuckDB `reject_errors` fields: `column_idx` + verbatim `csv_line` (byte offsets deliberately not adopted — stdlib csv exposes none). 107 tests.
- **Charset detection + ERR-07 + load-time hardening DONE (2026-07-21; taxonomy 0.3, runtime 0.6.0, compiler 0.4.0):** (1) ENC-01 non-UTF-8 bytes now get RANKED candidate encodings (shift_jis/euc_jp/gb18030/cp1252/latin-1) scored by decoded-text plausibility (C1-control fingerprint discriminates CJK-misread-as-Latin-1); still ask-class, evidence carries all candidates; the JP corpus files now profile as shift_jis with readable headers. (2) **ERR-07** (new taxonomy entry, minor bump to 0.3, copies synced): unexpected row-level exception quarantines the row and continues (raw preserved, csv_line intact); ERR-02 end-of-run/all-rows-failed promotes systemic breakage to exit 1; fail-fast honors first-occurrence abort; annotate NEVER repairs an unattributable failure into clean output (whole row quarantines). (3) Load-time control-char rejection in ALL spec strings (incl. U+2028/29) with a path-precise SpecError — comment injection now impossible by construction; `expr.python` stays the one exempt, sanctioned channel; `_cmt` remains defense-in-depth.
- **Review backlog CLOSED (2026-07-23; taxonomy 0.4, runtime 0.7.0, compiler 0.5.0, spec format 0.4):** (1) **ENC-08** spreadsheet formula injection (new taxonomy entry — ENC-07 was already taken by literal escapes): ask-class, default pass-through-and-flag; opt-in `policies.formula_injection: neutralize` prefixes `'` to at-risk output cells at render time (`=`/`@` always; `+`/`-` only with a non-numeric tail — plain signed numerics and bare dashes never touched), counted per column per ERR-04; profiler detects formula-leading source values (grouped ask); unknown policy value = SpecError + RunError; the key is REQUIRED in spec ≥ 0.4, pre-0.4 specs compile with the pass-through default. (2) **Datetime detection**: profiler strips a time/zone tail before date analysis, so `08.08.2018 00:00` now fires TYP-03, and naive datetimes (incl. ISO date+time) fire a new TYP-04 ask; offset/Z-aware values don't. Dotted dates now require a 4-digit year (`1.2.10` version numbers no longer TYP-03). (3) **Report durability**: all reports (errors/changes/quarantine/summary/manifest) go through one `_atomic_write` (temp + flush + fsync + replace) plus a best-effort directory fsync — a crash can no longer leave a truncated summary.json. (4) Profiler noise: `FOOTER_KEYWORDS` word-boundary ("Totally Organic" ≠ total row); `rows_profiled` now counts only non-blank data rows below the true header. 158 tests.
- **Eval iteration 3 DONE (determinism — first separation): skill 4/4 properties, baseline 0/4.** `evals/iteration-3/{results.json,benchmark.md}`; analysis in `docs/eval-report-iteration-3.md` [local-only]. E1 regen×3: skill byte-identical 3/3; baseline 0/3 pipelines/report-schemas (but 3/3 output VALUES — the spec alone pins data; the skill's margin is the operational contract + zero-model regen). E2 authoring×3: skill decision- and output-identical 3/3; baseline 0/3 (dup kept/dropped/kept, ragged padded 3/3, JSON report 1/3). E3 unseen variants: skill 4/4 exact taxonomy codes; a baseline misassigned 2/4 plausible-looking codes. E4 edit: skill 1-line diff + ERR-02 catches a wrong-for-data edit; baseline's clean edit sits on a runtime reading the same budget spec oppositely. **Compiler-coverage limit found:** 2/3 authoring runs hand-generated (ENC-06 repair + STR-06 footer-skip outside compiler vocabulary) — see next steps.

- **Eval iteration 4 DONE (determinism regression on the new stack): 4/4 properties held, 3/3 it-3 honest limits closed, 2/2 new capabilities to spec.** `evals/iteration-4/{results.json,benchmark.md,eval_metadata.json,grade_e*.py}`; analysis in `docs/eval-report-iteration-4.md` [local-only]. Baselines deliberately NOT rerun (they never touch the stack; it-3 baseline 0/4 stands — rerun them only if prompts/inputs change). E1: byte-identity 3/3 for BOTH specs, incl. the 17-trap spec that was outside compiler vocabulary in it-3. E2 (3 fresh isolated Opus agents, unattended): decisions/outputs/summaries 3/3 with *identical full warning maps*; compiler-first 3/3 (was 1/3), canonical names 3/3, ENC-06 counted 3/3; one run self-corrected a forgotten footer rule after reading its own quarantine report. E3: 4/4 exact codes. E4: it-3 edits reproduce + annotate edit (1 policy line → load+ledger) + formula-neutralize edit (spec 0.4 → ENC-08 counted, signed numerics safe). E5 probe: 8/8 new detections, no false positives. Residual wobble: unattended provenance labels varied (default vs unconfirmed) — SKILL.md tightening candidate (it-5 list in the report).

## Code-review backlog (2026-07-21 multi-agent review)

**CLOSED 2026-07-23** (see current state): formula injection (landed as **ENC-08** — the
review note said "e.g. ENC-07" but that ID was already taken by literal escapes), datetime
detection, report atomicity+fsync, and the minor profiler noise items are all done. Still
open, deliberately:

- Streaming (multi-GB inputs) stays a non-goal (whole-file read); revisit only if needed.
- The runtime header-scan block (`missing`/`extra`/`col_index`/dup-headers)
  is O(cols²) — fine at tens, seconds at thousands; de-quadratic with a
  `Counter(header)` if wide files become real.

## Prioritized next steps

5. **Extract the runtime into a proper package** (`pip install etl-compiler-runtime`) — the unit-test suite now exists (`tests/`), so packaging is the remaining step.
6. **TypeScript runtime + target**; **SQL/dbt target** (analyst-serving surface). Sequenced after the Python path is solid.

Test suite lives in `tests/` (`python3 -m unittest discover -s tests`) — runtime + profiler + compiler + **the type gate** (`tests/test_typecheck.py` runs mypy; **all product modules strict** — runtime `CellValue`/config/record contracts, compiler `SpecDict`/typed-YAML-loader, profiler `Finding`/`InterviewGroup`/`ProfileResult`; validate-then-cast at checked boundaries; tests stay at default+check_untyped_defs by design). Dev setup: `uv sync --group dev` (mypy lives in the uv dev group; the gate fails with instructions if missing — `ETL_SOLVED_SKIP_TYPECHECK=1` bypasses in minimal envs). Keep it green. Corpus audit ([local-only]): `python3 corpus/audit.py` after profiler changes.

## Things to NOT do

- Don't add hosted execution, connectors, nested-data support, or multi-table joins yet (PRD non-goals still stand; spec format was designed so joins/nesting can arrive without rewrites — namespaced, path-like source refs).
- Don't let generated pipelines grow inline edge-case logic. If the runtime lacks something, extend the runtime.
- Don't change taxonomy defaults casually — that's a major-version event affecting every spec that relied on them.
