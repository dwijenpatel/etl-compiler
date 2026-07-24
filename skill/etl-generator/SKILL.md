---
name: etl-generator
description: Interactive ETL code generator built on a failure-mode taxonomy. Use this skill whenever the user wants to transform, map, convert, migrate, load, or clean tabular data (CSV/TSV) from one structure to another, wants ETL or data-pipeline code written, mentions mapping source data to a target schema/format, or asks for "a script to convert this file" — even if they never say "ETL". Also use it when the user provides an existing mapping spec (.etlspec.yaml) and wants the pipeline regenerated or modified, or when they complain about data quality issues (bad dates, nulls, weird characters, encoding problems) in a file they need to process. It profiles samples, interviews the user only about issues actually detected, records every decision in an auditable spec, and generates hardened Python whose edge-case handling lives in a tested runtime module.
compatibility: Requires Python 3 (standard library only — no third-party packages) and the ability to run bundled scripts. Harness-agnostic; conforms to the Agent Skills open standard.
metadata:
  taxonomy-version: "0.4"
  runtime-version: "0.7.0"
---

# ETL Generator

Generate correct, conformant ETL code from sample data — by walking a failure-mode taxonomy instead of improvising.

## Why this skill exists

Hand-written ETL fails in the same recurring ways: nulls and sentinel values, encoding damage, ambiguous dates, silent truncation, inconsistent error handling. One-shot code generation has the same disease — plausible handling, different every time. This skill fixes both by making every edge-case decision **explicit, finite, and recorded**, and by generating code whose hardened semantics live in one shared runtime module (`etl_runtime.py`) rather than being re-improvised per pipeline.

Three artifacts always come out of this workflow:
1. **The spec** (`<name>.etlspec.yaml`) — every mapping and every policy decision, with provenance
2. **The pipeline** (`<name>_pipeline.py`) — thin, readable orchestration that imports the runtime
3. **The run report** — produced by executing the pipeline on the sample: per-row errors, per-error-type aggregates, run summary

## Workflow

### Step 1 — Gather inputs

Required: a sample of the **input** data (CSV/TSV file, or pasted rows).
Strongly preferred: a sample of the **desired output**, and/or a target schema (SQL DDL, JSON Schema, column list). If only a verbal description of the target exists, elicit column names, types, and nullability before proceeding.

### Step 2 — Profile

Run the bundled profiler (`scripts/profile.py`, relative to this skill's directory) on the input sample (and the output sample if provided):

```bash
python3 scripts/profile.py input_sample.csv --json findings.json
```

It emits findings keyed by taxonomy IDs (see `references/taxonomy.md`), each with evidence and counts. Read the findings before talking to the user — the profiler output determines the entire interview.

The output has two views of the same findings: `findings` (one per column, full detail) and `interview_groups` (homogeneous `ask` findings collapsed into one question each — e.g. 40 columns that all look like `Y/N` booleans become a single group). **Drive the interview from `interview_groups`, not `findings`** — ask one question per group ("these 40 columns look like Y/N — confirm the mapping applies to all; call out any exceptions"), and let the user override individual columns. This keeps wide files (hundreds of columns) to a handful of questions instead of hundreds. `summary.ask_questions` is the number of questions the interview will actually pose.

### Step 3 — Interview (detection-driven, never exhaustive)

Consult `references/taxonomy.md` for each finding's decision space, default, and class:

- `fix`-class findings: apply the default silently, but record each in the spec and mention them in one summary line ("I'll strip the BOM, normalize unicode to NFC, and trim whitespace — all counted in run reports").
- `ask`-class findings: these are meaning-changing. Ask the user, showing the profiler's evidence ("`order_date` is ambiguous: all 240 values parse as both MDY and DMY — which is it?"). Batch related questions and pose them together (drive this from the profiler's `interview_groups`, which collapses identical per-column decisions into one question); if the harness offers a structured or multiple-choice question interface, use it, otherwise ask in plain text. Never guess on: date format ambiguity (TYP-03), decimal locale (TYP-02), sentinel values (NUL-03), timezone of naive datetimes (TYP-04).
- Findings absent from the profile: take the default, record it with provenance `default`. Do not ask about them.

**Unattended mode:** if there is no user to interview (batch/CI context, or the user has said "just make reasonable choices"), do not block. Choose the safest option for each `ask`-class finding (the one that quarantines rather than reinterprets data), mark every detected `ask`-class decision `provenance: unconfirmed` — even when the option chosen is the house default; `default` is reserved for findings the profiler did not detect — and list them prominently at the end so a human can review.

### Step 4 — Propose mappings

Map input columns to output columns: direct copies, renames, casts, reformatting, splits/concats, constants. Present the full mapping table with a confidence note per mapping and the list of unmapped output columns / unused input columns. Let the user correct anything before proceeding.

### Step 5 — Write the spec

Write `<name>.etlspec.yaml` following `references/spec-format.md` exactly, where `<name>` is the spec's `name` field — snake_case, derived from the input filename's stem unless the user supplies a name (e.g. `orders_export.csv` → `orders_export.etlspec.yaml`, later `orders_export_pipeline.py`). Consistent naming keeps independent regenerations byte-comparable. The spec must contain a decision (with provenance) for every policy the taxonomy defines — including defaulted ones. Show it to the user for approval. The spec, not the conversation, is the source of truth; if the user later wants changes, edit the spec and regenerate.

### Step 6 — Generate the pipeline (compiler-first; do not hand-write code)

1. Copy `assets/etl_runtime.py` **and** `assets/etl_coercers.py` into the user's project directory (unmodified — together they are the tested, shared implementation of taxonomy semantics; never inline or fork their logic into the pipeline). The runtime is deliberately split on the effect boundary: `etl_coercers` is the deterministic core, `etl_runtime` the I/O driver that re-exports it — pipelines import only `etl_runtime`.
2. Run the bundled deterministic compiler — **the spec is your output; the pipeline is the compiler's**:

```bash
python3 scripts/compile_spec.py <name>.etlspec.yaml -o <name>_pipeline.py
```

Identical spec bytes always produce a byte-identical pipeline (the header embeds the spec's sha256), so regeneration is reviewable as a diff of the *spec*, never of code.

If the compiler raises a `SpecError`, the spec — not the compiler — is wrong or incomplete: fix the spec (the error names the exact rule, e.g. a missing policy key or an undeclared unfilled column) and recompile. Only if the spec genuinely needs a transform the compiler does not yet support (it will say so) fall back to hand-generating per `references/codegen-guide.md`, preferring the `{op: expr}` escape hatch over new inline logic.

Every generated error path uses taxonomy IDs as error codes. Every mapping carries a comment tracing it to the spec.

### Step 7 — Verify before declaring done

Always execute the generated pipeline against the input sample:

```bash
python3 <name>_pipeline.py input_sample.csv --out-dir ./etl_out
```

Check: the run completes; the report is written; quarantine counts match expectations from profiling; and if an output sample was provided, diff the produced output against it and reconcile every discrepancy (a discrepancy is either a bug to fix or an undocumented decision to surface to the user). Show the user the run summary — it is the best demonstration that the pipeline works.

## Rules that keep this trustworthy

- **Never guess meaning-changing ambiguity.** A wrong BOM decision is annoying; a wrong MDY/DMY decision silently corrupts every row. The taxonomy's `ask` class exists for exactly this line.
- **Every decision lands in the spec.** If a behavior isn't in the spec, it shouldn't be in the code. Defaults are decisions too — record them.
- **The runtime is the only place edge-case semantics live.** If a needed behavior is missing from `etl_runtime.py`, add it there (with the taxonomy ID in comments), not inline in the pipeline. The runtime is strict-typed (mypy `--strict`) and split on the effect boundary: a deterministic core (`etl_coercers`, whose only effect is counting fixes into the passed report) and a thin I/O driver — keep additions to that standard.
- **Reports are non-negotiable.** Per-row errors, per-error-type aggregates, and the run summary are always all produced (taxonomy ERR-03). Auto-fixes are counted, never silent (ERR-04).
- **Same spec → same behavior.** Regeneration from an unchanged spec must not change pipeline behavior.

## Bundled resources

- `references/taxonomy.md` — the failure-mode taxonomy: IDs, detection, decision spaces, defaults, classes. Read the relevant entries during the interview; this is the skill's brain.
- `references/spec-format.md` — the .etlspec.yaml schema with a complete example. Read before writing any spec.
- `references/codegen-guide.md` — pipeline structure, runtime API usage, and codegen conventions. Read before generating code.
- `scripts/profile.py` — the profiler; run it, don't reimplement it.
- `assets/etl_runtime.py` + `assets/etl_coercers.py` — the two-file runtime; copy both into the user's project verbatim.
