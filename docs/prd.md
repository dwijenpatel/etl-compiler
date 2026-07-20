> **Historical note (2026-07-17):** This PRD captures the original *product* framing (standalone web app, adoption/revenue metrics). The project has since pivoted — see `docs/brainstorm-log.md` and `CLAUDE.md`: no monetization goal; skill-first distribution (Claude Code skill + shared runtime library); the failure-mode taxonomy (`docs/taxonomy.md`) is the center of gravity. The PRD remains valuable for scope decisions (flat-tabular v1, optional schemas, spec-as-source-of-truth) and the P1/P2 backlog.

# PRD: AI-Enabled ETL Code Generator

**Working name:** MapForge (placeholder)
**Author:** Dwijen Patel
**Date:** July 17, 2026
**Status:** Draft v1.1 (adds optional schema ingestion)

---

## Summary

A tool that turns *examples* into *transformation code*. The user provides a sample input table and a sample of the desired output table; either side may **optionally** be accompanied by an exported schema (SQL DDL, JSON Schema, or similar), which pins down exact types, nullability, and constraints that sample rows alone can't reveal. AI proposes a column-level mapping/transformation between them. The user refines the mapping — through AI chat or direct manual editing — until it's right, then the tool generates clean, portable Python or TypeScript that the user runs in their own environment.

The product ships as a standalone web app **and** a CLI, both built on the same composable core engine. The central artifact is a **mapping spec**: a declarative, versionable intermediate representation that the AI proposes, the user edits, and the code generator deterministically compiles.

---

## Problem Statement

Writing data transformation code is high-frequency, low-creativity work. Data engineers, analytics engineers, and application developers routinely spend hours hand-writing column mappings, renames, type casts, date reformatting, string splits, and null handling — logic that is fully determined by "what the data looks like now" and "what it needs to look like." The knowledge of the *intent* is cheap; expressing it as correct, tested code is expensive.

Existing options force a bad trade: visual ETL tools are opaque, hard to version-control, and lock users into a runtime; fully hand-written code is slow and boilerplate-heavy; and raw LLM prompting ("write me a script that...") produces plausible-looking code with no structured way to inspect, correct, or trust the mapping before running it. The cost of not solving this is measured in engineering hours burned on boilerplate and in silent data bugs from mappings nobody reviewed field-by-field.

---

## Goals

1. **Cut time-to-working-code dramatically.** A user with input/output samples in hand gets exported, runnable code in **under 15 minutes median** (vs. hours of hand-writing).
2. **Make the AI proposal trustworthy enough to start from.** On flat tabular benchmarks, **≥ 70% of proposed column mappings are accepted without edits**.
3. **Generate code users actually keep.** Generated code is idiomatic, dependency-light, and portable — **≥ 60% of exporting users report using the code with zero or minor modifications** (survey + telemetry proxy).
4. **Prove the composable architecture.** Web app and CLI ship on the same core engine, with the mapping spec as a stable contract — validated by the CLI reaching **≥ 20% of weekly active users** within 90 days (signals real workflow integration, not just demo usage).
5. **Earn repeat usage.** **≥ 40% of users who export code return for a second session within 30 days** — the tool becomes part of how they do transformation work, not a one-time trick.

---

## Non-Goals (v1)

1. **Nested / semi-structured data (JSON, XML, arrays).** Flat tabular only. Nested data multiplies mapping complexity (flattening, explosion, path expressions) and would slow v1 significantly. Architectural insurance only (see P2).
2. **Hosted execution, scheduling, or orchestration.** We generate code; the user runs it. Building a runtime turns this into an ETL platform — a different product with different economics. Explicitly out.
3. **Live connections to databases/warehouses.** Samples — and optional schema exports — arrive as uploaded files or pasted text, not via connectors. Connectors are a large surface area (auth, drivers, security review) that doesn't change the core value proposition.
4. **Multi-table inputs and joins.** V1 is one input table → one output table. Joins are the most-requested likely extension, but they double the inference and editor complexity; they're first in line for v2 (P1/P2).
5. **Output languages beyond Python and TypeScript.** No SQL/dbt, Java, or Scala targets in v1. The spec-based architecture makes new targets cheap later; two languages is enough to validate demand.
6. **Data profiling / quality suite.** We infer types and flag anomalies only as far as needed to propose mappings. A full profiling product is a separate initiative.

---

## Users & Personas

- **Data engineer (primary).** Writes pipelines for a living. Wants to skip boilerplate, will read every line of generated code, and will judge the product by code quality and CLI/CI fit.
- **Analytics engineer / analyst (primary).** Knows the data semantics cold; SQL-fluent but not a software engineer. Wants the chat and editor to carry them; the generated Python is something they run more than they read.
- **Software engineer, non-data (secondary).** Occasionally needs to move data between systems (e.g., migrating a customers table between SaaS exports). Wants TypeScript that drops into a Node service with types. Doesn't want to learn ETL tooling.

---

## User Stories

### Core flow (all personas)

- As a data engineer, I want to upload a sample input file and a sample of my desired output so that the system infers the transformation instead of me writing boilerplate.
- As a data engineer, I want to optionally attach an exported schema (CREATE TABLE DDL, JSON Schema, or similar) alongside either sample so that types, nullability, and constraints are exact rather than inferred — and the generated code defends against edge cases my sample rows don't happen to contain.
- As a data engineer, I want to review every proposed mapping in a structured editor — source column(s), transformation, target column, confidence — so that I can verify field-by-field before trusting the output.
- As an analytics engineer, I want to tell the AI in plain language how to fix a mapping ("that date is DD/MM/YYYY, not MM/DD" or "split full_name into first_name and last_name, title-cased") so that I don't have to write the expression myself.
- As a data engineer, I want to manually edit any mapping expression directly so that I'm never blocked waiting on chat when I already know exactly what I want.
- As a data engineer, I want to export idiomatic, commented Python so that the code drops into my existing pipeline with minimal dependencies.
- As a software engineer, I want TypeScript output with types derived from the input/output schemas so that the transform integrates into my Node service with compile-time safety.

### CLI & composability

- As a data engineer, I want a CLI that takes sample files and emits a mapping spec and generated code so that I can use this from my terminal and wire it into scripts and CI.
- As a data engineer, I want the mapping spec saved as a readable JSON/YAML file so that I can diff it, version-control it, and regenerate code from it deterministically.

### Edge cases & trust

- As a user, I want output columns the AI couldn't map clearly flagged (and unused input columns listed) so that I know exactly what needs my attention before export.
- As a user, I want low-confidence or ambiguous mappings visually distinguished from confident ones so that I review the risky ones first.
- As a user, I want a clear, specific error when my sample file can't be parsed (bad delimiter, ragged rows, encoding) so that I can fix it and retry.
- As a user, I want conflicts between an attached schema and my sample data (e.g., a value that violates a declared type or NOT NULL) flagged before the proposal runs so that I always know which source of truth is in effect.
- As a user, I want generated code to fail loudly and informatively at runtime (missing column, uncastable value) so that silent data corruption can't happen.

---

## Requirements

### Must-Have (P0)

**P0.1 — Sample ingestion (flat tabular), with optional schemas**
Upload or paste input and desired-output samples as CSV/TSV. Parse headers, infer column types (string, int, float, bool, date/datetime, nullable-ness) from sample rows. **Optionally**, either side may be accompanied by an exported schema artifact. V1 parses SQL `CREATE TABLE` DDL and JSON Schema natively; any other schema text (Avro, dbt `schema.yml`, ORM/pydantic models, vendor exports) is normalized by AI into the internal schema model and shown to the user for confirmation. When a schema is present, it is the **source of truth** for types, nullability, and constraints (enums, lengths, ranges); sample-based inference fills only what the schema doesn't cover. Enforce sane limits (e.g., ≤ 10 MB per file; inference uses up to ~1,000 rows).

*Acceptance criteria:*
- [ ] User can provide input and output samples via file upload and paste
- [ ] User can attach a schema for input, output, both, or neither — the flow is identical with none
- [ ] Headers and types are displayed for confirmation before inference runs, distinguishing schema-declared facts from sample-inferred ones
- [ ] Attached schemas override sample inference for types, nullability, and constraints
- [ ] Sample values that violate an attached schema are flagged (column, row, value) before the proposal runs
- [ ] Schema text that native parsers can't handle falls back to AI-assisted normalization with explicit user confirmation — never a silent guess
- [ ] Malformed files produce a specific, actionable error (row number, likely cause) — never a generic failure
- [ ] Files exceeding limits are rejected with the limit stated

**P0.2 — AI mapping proposal**
Given both samples (and any attached schemas), propose a column-level mapping spec covering: direct copy, rename, type cast, date/number reformatting, string operations (split, concat, case, trim, regex extract), constants/defaults, and simple derived expressions. Every mapping carries a confidence level and a one-line plain-language explanation. Declared types and constraints are used to disambiguate candidate mappings and to calibrate confidence.

*Acceptance criteria:*
- Given valid input and output samples, when the user requests a proposal, then every output column receives either a proposed mapping or an explicit "unmapped" flag
- Given the proposal is displayed, when the user inspects any mapping, then they see source column(s), the transformation, a plain-language explanation, and a confidence indicator
- [ ] Unused input columns are listed separately
- [ ] Proposals never contradict an attached schema (e.g., no mapping emits a type or value the output schema forbids)
- [ ] Proposal completes in ≤ 30 seconds for samples within limits

**P0.3 — Manual mapping editor**
A structured editor showing all mappings. Users can edit the transformation for any target column, change source columns, add/delete mappings, and set constants — without touching chat.

*Acceptance criteria:*
- [ ] Every element of the mapping spec is viewable and editable in the UI
- [ ] Edits validate immediately against the sample schemas (unknown column → inline error)
- [ ] Manual edits and AI-chat edits operate on the same spec — no divergence

**P0.4 — AI chat refinement**
A chat panel where natural-language instructions modify the mapping spec. Chat responses state what changed; changes appear in the editor immediately. The spec — not the conversation — is the source of truth.

*Acceptance criteria:*
- Given an existing proposal, when the user issues an instruction affecting one or more mappings, then the affected mappings update in the editor and the chat reply summarizes exactly what changed
- Given an ambiguous instruction, when the AI cannot resolve it confidently, then it asks a clarifying question rather than guessing silently
- [ ] User can undo the last chat-applied change

**P0.5 — Deterministic code generation (Python & TypeScript)**
Compile the finalized spec into code. Python: pandas-based, or stdlib-only where feasible. TypeScript: typed row interfaces derived from the schemas. Code is idiomatic, commented (each mapping traceable to a comment), and includes runtime guards: missing-column checks, cast-failure handling with row-level error reporting. When schemas are attached, generated code enforces the **full declared contract**, not just what the sample exhibits — null handling on nullable columns even if the sample contains no nulls, enum/range/length validation where declared, and explicit defaults where the schema specifies them. Same spec → same code, every time.

*Acceptance criteria:*
- [ ] Generated Python runs against the original input sample and produces the mapped output without modification
- [ ] Generated TypeScript compiles under strict mode without modification
- [ ] Regenerating from an unchanged spec is byte-identical
- [ ] Runtime failures identify the column, row, and value that failed
- [ ] With schemas attached, generated code validates declared constraints (nullability, enums, lengths/ranges) even where the sample never exercises them
- [ ] Dependencies are limited to a small documented set (e.g., pandas only for Python)

**P0.6 — Export & the mapping spec as a first-class artifact**
Download generated code and the mapping spec (JSON or YAML). The spec is human-readable, documented, and sufficient to regenerate code without the samples.

*Acceptance criteria:*
- [ ] One-click download of code file(s) and spec; copy-to-clipboard for code
- [ ] Spec format is versioned (schema version field) from day one

**P0.7 — CLI on the same core**
A CLI exposing the pipeline: `propose` (samples → spec), `generate` (spec → code in chosen language), and `refine` via flags/spec editing. The web app and CLI consume the same core library.

*Acceptance criteria:*
- [ ] `mapforge propose --input in.csv --output out.csv` emits a spec file
- [ ] `mapforge generate --spec spec.yaml --lang python|typescript` emits code identical to what the web app produces from the same spec
- [ ] CLI is distributable via standard package manager (npm/pip/Homebrew — pick one for v1)

### Nice-to-Have (P1)

- **P1.1 — In-product preview.** Execute the finalized transform against the sample rows and show the result diffed against the provided desired output. Highest-leverage trust feature; deferred from P0 because it requires a sandboxed execution path. Strong fast-follow candidate.
- **P1.2 — Session persistence.** Save and reload mapping projects (samples + spec + chat history); spec version history.
- **P1.3 — Richer transformation library.** Row filtering, dedup, simple aggregations, pivot/unpivot.
- **P1.4 — More sample and schema formats.** Samples: Excel (.xlsx), Parquet, pasted Markdown tables. Native schema parsers: Avro, Parquet metadata, dbt `schema.yml`, pydantic/pandera models (AI-assisted normalization covers these in v1; native parsing makes them deterministic and offline-capable).
- **P1.5 — Generated tests.** Emit a unit test file using sample rows as fixtures, so the code arrives with its own regression check.

### Future Considerations (P2)

- **P2.1 — Multi-table inputs with joins.** Design the spec so a mapping's source can later reference multiple tables (namespace source columns from day one).
- **P2.2 — Nested/semi-structured data.** Spec's source/target references should be path-like strings, not bare column names, so JSON paths slot in later.
- **P2.3 — Additional targets: SQL/dbt.** Spec-to-code compilation is target-pluggable; keep transformation semantics engine-agnostic (avoid baking pandas semantics into the spec).
- **P2.4 — Warehouse/database schema introspection connectors.**
- **P2.5 — Team features.** Shared spec libraries, review/approval flows on spec diffs.

---

## Success Metrics

### Leading indicators (evaluate at 2 and 6 weeks post-launch)

| Metric | Definition | Success | Stretch |
|---|---|---|---|
| Activation | % of new sessions that upload both samples and receive a proposal | 60% | 75% |
| Proposal acceptance | % of proposed mappings finalized without edit (telemetry: edit events per mapping) | 70% | 85% |
| Completion | % of sessions with a proposal that reach code export | 50% | 65% |
| Time to export | Median, sample-upload → first code export | < 15 min | < 8 min |
| Chat efficacy | % of chat instructions producing an accepted spec change (no immediate undo/re-edit) | 65% | 80% |
| Parse failure rate | % of sample uploads failing to parse | < 10% | < 5% |

### Lagging indicators (evaluate at 30 and 90 days)

| Metric | Definition | Success | Stretch |
|---|---|---|---|
| Retention | % of exporting users with a 2nd session within 30 days | 40% | 55% |
| CLI adoption | % of WAU using the CLI | 20% | 35% |
| Code survival | % of surveyed users who used exported code with zero/minor changes | 60% | 75% |
| Satisfaction | Post-export CSAT (1–5) | ≥ 4.0 | ≥ 4.5 |

**Measurement notes:** proposal-acceptance requires per-mapping edit telemetry from day one — instrumenting this is a launch requirement, not a follow-up. Accuracy targets should also be validated pre-launch against a curated offline benchmark suite (see Open Questions). Additionally, track **schema attach rate** and split proposal-acceptance by with/without schema: the core hypothesis is that schemas measurably reduce ambiguity, and this split is how we confirm it (and how we decide how hard to push schema attachment in the UX).

---

## Open Questions

**Blocking (answer before build starts):**

1. **[Engineering/Legal] What data reaches the LLM?** Do we send raw sample rows, or schemas + a redacted/synthesized handful of values? Samples will contain PII. This decision shapes architecture, the privacy policy, and enterprise viability. Related: retention policy for uploaded samples.
2. **[Engineering] Codegen strategy: templates vs. LLM.** Determinism (P0.5) strongly implies template/AST-based generation compiled from the spec, with the LLM confined to proposal and chat. Confirm this architecture — it's the difference between a trustworthy compiler and a flaky one.
3. **[Engineering/Design] Expression language for the spec.** What can a mapping expression contain? A closed set of built-in transformations keeps the editor safe and both codegen targets consistent; raw Python/TS snippets are more powerful but break cross-language generation and safety. Recommend: closed transformation set for v1, with an explicit "custom expression" escape hatch that's language-specific and clearly marked.
4. **[Data] Benchmark suite.** We need ~50–100 curated input/output sample pairs (varied domains, messiness levels) to measure proposal accuracy before and after launch. Who builds and owns this?

**Non-blocking (resolve during build):**

5. **[Design] How is confidence communicated in the editor** without either alarming users or being ignored?
5b. **[Engineering] Native schema parser breadth.** SQL DDL varies by dialect (Postgres, MySQL, Snowflake, BigQuery). How many dialects get native parsers in v1 vs. leaning on AI-assisted normalization + confirmation? Recommend: one permissive DDL parser + JSON Schema natively, AI fallback for everything else, and let the schema-attach telemetry (see Success Metrics) prioritize native parsers post-launch.
6. **[Engineering] CLI distribution channel** (npm vs. pip vs. both) and whether the CLI calls a hosted API for proposal (it must, presumably — clarify offline behavior for `generate`, which can be fully local).
7. **[Product] Pricing/packaging.** Free tier limits, seat vs. usage pricing. Blocking for launch, not for build.
8. **[Product] Naming.** "MapForge" is a placeholder.

---

## Timeline Considerations

No hard external deadline identified. Suggested phasing (relative, pending engineering estimates):

**Phase 1 — Core engine + web alpha.** Spec format v1, sample ingestion, AI proposal, manual editor, Python codegen, export. *Internal/design-partner alpha. Proves the loop: samples → proposal → edit → working code.*

**Phase 2 — Full v1 surface.** AI chat refinement, TypeScript target, CLI (`propose`/`generate`), telemetry, benchmark-validated accuracy. *Closed beta.*

**Phase 3 — Launch + fast follows.** Polish, docs, pricing; begin P1.1 (in-product preview) and P1.2 (persistence) immediately post-launch.

**Dependencies:** LLM provider/model selection and the data-privacy decision (Open Q1) gate Phase 1. The spec format must be reviewed against P2.1–P2.3 before freeze — it's the contract everything else composes around, and it's the one thing that's expensive to change later.

---

## Appendix: Architecture Note (context for engineering)

The user-confirmed direction is **composable pieces**: a core engine (parsing/type inference → mapping spec → deterministic codegen) packaged as a library, with the web app and CLI as thin consumers. The mapping spec is the stable contract between AI and human, between sessions, and between surfaces. The spec embeds a **schema model** for both sides; each fact in it is tagged by provenance (schema-declared vs. sample-inferred), which is what lets codegen enforce the full declared contract and lets the UI show users what's known vs. guessed. AI is used in exactly three places — normalizing non-native schema formats, initial proposal, and chat refinement — and all three emit structured artifacts the user confirms (a schema model or spec edits), never code. Code generation is a deterministic compiler over the spec. This keeps trust auditable (review the spec, not the model) and makes new output languages and future input shapes (joins, nested data) additive rather than architectural rewrites.
