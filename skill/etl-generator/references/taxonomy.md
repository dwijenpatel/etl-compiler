# ETL Failure-Mode Taxonomy

**Version:** 0.2 (draft)
**Status:** Founding artifact for `etl-generator`
**Author:** Dwijen Patel
**Date:** July 17, 2026 (v0.1); July 20, 2026 (v0.2)

**Changelog**
- **v0.2** (2026-07-20, minor — additions only, no default changed): added **TYP-12**
  (magnitude/scale-suffixed numerics), from the corpus validation pass (NOAA damage
  fields). Validation findings, coverage gaps, and open default-change proposals are in
  [taxonomy-validation-report.md](taxonomy-validation-report.md). External evidence corpus:
  `~/repos/etl-evidence`.
- **v0.1** (2026-07-17): founding taxonomy, 40 entries.

---

## Purpose

This document enumerates the recurring ways ETL fails, and for each failure mode defines a **finite decision space** with a **documented house default**. It is the single source of truth that drives every other component of the system:

- The **profiler** detects which failure modes are actually present in a sample and asks the user only about those.
- The **spec** records the decision made for every mode — including defaulted ones — with provenance (`explicit`, `default`, or `detected-confirmed`).
- The **runtime library** implements each mode's handling exactly once, tested in one place.
- The **error reports** produced by generated pipelines use these IDs as error codes, so a quarantined row saying `TYP-03` traces directly back to this document.

The taxonomy is versioned. Adding a failure mode is a minor version; changing a default is a major version (it changes the behavior of pipelines that rely on defaults).

## Design Principles

1. **Finite decision spaces.** Every failure mode has a small, enumerable set of sane handling options. If a proposed entry has an open-ended decision space, it is not yet understood well enough to be in the taxonomy.
2. **No silent decisions.** Defaults exist so users aren't interrogated about 40 things, but every applied default is recorded in the spec and counted in run reports. Silence is a bug.
3. **Detection-driven elicitation.** The interview only surfaces decisions for failure modes the profiler actually found evidence of. Everything else takes the house default.
4. **Never guess on ambiguity that changes meaning.** Format ambiguity (MDY vs DMY dates) is never resolved by default — it always requires confirmation. Mechanical cleanup (BOM stripping) is safely defaulted.
5. **Auto-fixes are counted.** A fix applied silently at runtime (trimmed whitespace, normalized unicode) is still tallied per column in the run report. Correct and invisible is not the same as correct and auditable.
6. **Policies compose.** Decisions apply at dataset level with per-column overrides. The spec records the effective policy per column.

## Entry Format

Each entry has: **ID** (stable, used as runtime error/warning code), **What** (the failure), **Detect** (profiler signal), **Options** (the decision space), **Default** (house style), and **Class** — one of:

- `fix` — auto-repairable; applied per policy, counted as a warning
- `ask` — meaning-changing; profiler detection always triggers a question
- `row-error` — affects individual rows at runtime; disposition governed by ERR-01
- `run-error` — invalidates the run; always fails fast

---

## ENC — Encoding & Characters

**ENC-01 · Encoding mismatch**
- What: File is not the expected encoding (Latin-1 masquerading as UTF-8, etc.); decode errors or mojibake result.
- Detect: BOM inspection; strict-UTF-8 decode attempt; byte-distribution heuristics on failure.
- Options: (a) require declared encoding, fail on decode error; (b) detect and decode with detected encoding; (c) decode with replacement characters.
- Default: (b) — detect, decode, record detected encoding in spec; decode failures beyond detection are run-errors. Never (c) silently: replacement chars are data loss.
- Class: `ask` when detection is low-confidence; otherwise `fix`.

**ENC-02 · Byte-order mark (BOM)**
- What: UTF-8/UTF-16 BOM prepended; contaminates first header name (`﻿id`).
- Detect: First bytes of file.
- Options: (a) strip; (b) preserve; (c) fail.
- Default: (a) strip and record.
- Class: `fix`.

**ENC-03 · Unicode normalization inconsistency**
- What: Same visible string in mixed forms (composed é U+00E9 vs decomposed e+U+0301); breaks joins, dedup, comparisons.
- Detect: Values change under NFC normalization.
- Options: (a) normalize NFC; (b) normalize NFKC (also folds compatibility chars: ﬁ→fi, ² → 2); (c) leave as-is.
- Default: (a) NFC. NFKC only by explicit choice — it changes content.
- Class: `fix`.

**ENC-04 · Control characters & non-printables**
- What: NUL, vertical tabs, 0x00–0x1F, 0x7F embedded in values; breaks downstream parsers and databases.
- Detect: Scan for control-range codepoints (excluding legitimate \t in unquoted TSV context).
- Options: (a) strip; (b) replace with space; (c) keep; (d) row-error.
- Default: (a) strip, counted per column.
- Class: `fix`.

**ENC-05 · Unicode whitespace & invisible characters**
- What: Non-breaking space (U+00A0), zero-width space/joiner, directional marks; visually identical values that don't compare equal; trims that don't trim.
- Detect: Scan for non-ASCII whitespace and zero-width codepoints.
- Options: (a) map unicode whitespace → ASCII space and strip zero-width chars; (b) keep.
- Default: (a), counted per column.
- Class: `fix`.

**ENC-06 · Mojibake (prior mis-decode baked into data)**
- What: Artifacts like `Ã©` for `é`, `â€™` for `'` — a previous pipeline's encoding error stored as real characters.
- Detect: Signature byte-sequence patterns of double-encoded UTF-8.
- Options: (a) attempt repair (ftfy-style round-trip); (b) pass through; (c) row-error.
- Default: (b) pass through and **flag** — repair is opt-in because heuristic repair on non-mojibake corrupts data.
- Class: `ask` when detected.

**ENC-07 · Literal escape sequences & entities**
- What: Values contain literal `\n`, `\t`, `\"`, or HTML entities (`&amp;`, `&#39;`) that may or may not be intended as escapes.
- Detect: Pattern frequency across a column (occasional = likely content; pervasive = likely encoding).
- Options: (a) unescape; (b) keep verbatim.
- Default: (b) keep, flag for confirmation when pervasive.
- Class: `ask` when pervasive; otherwise none.

---

## STR — Structure & Parsing

**STR-01 · Delimiter ambiguity**
- What: Wrong or inconsistent delimiter; commas inside unquoted values; the "European CSV" (semicolon-delimited).
- Detect: Dialect sniffing across sample rows; field-count stability under candidate delimiters.
- Options: delimiter is a declared spec value; sniffed value requires confirmation when confidence is low.
- Default: sniff + confirm once; thereafter declared in spec, deviations are row-errors.
- Class: `ask` at spec time; `row-error` at runtime.

**STR-02 · Ragged rows**
- What: Row has more or fewer fields than the header.
- Detect: Field-count histogram.
- Options: (a) row-error; (b) pad short rows with nulls / truncate long rows; (c) run-error.
- Default: (a) quarantine. Padding/truncating silently invents or destroys data.
- Class: `row-error`.

**STR-03 · Quoting violations**
- What: Unescaped quotes, embedded newlines in unquoted fields, mixed quoting styles; parser produces shifted fields.
- Detect: Parse with strict dialect; count parse failures; look for tell-tale shifted-column type violations.
- Options: (a) strict parse, unparseable rows are row-errors; (b) lenient parse with recovery heuristics.
- Default: (a). Lenient recovery is opt-in and its heuristics must be named in the spec.
- Class: `row-error`.

**STR-04 · Header anomalies**
- What: Missing header row, duplicate column names, blank header cells, leading/trailing whitespace or newlines in names, header repeated mid-file (concatenated exports).
- Detect: Header vs. first-rows type analysis; duplicate/blank name scan; header-pattern recurrence scan.
- Options: per anomaly — declare header absent (names supplied in spec); dedupe names with suffixes + confirm; mid-file header rows as row-errors.
- Default: whitespace-trim names (`fix`); duplicates and missing headers always `ask`; mid-file headers quarantined.
- Class: mixed, as noted.

**STR-05 · Duplicate rows**
- What: Exact or key-based duplicate records.
- Detect: Exact-duplicate count in sample; key-duplicate count if key declared (see KEY-01).
- Options: (a) keep all; (b) drop exact duplicates; (c) key-based dedup with first/last/fail resolution.
- Default: (a) keep, report count. Dedup changes semantics; it must be chosen.
- Class: `ask` when detected above threshold; runtime per spec.

**STR-06 · Non-data rows (blanks, footers, preamble)**
- What: Fully blank rows; report-style preamble above the header; footer rows ("Total: 1,234", generated-by lines).
- Detect: Blank-row positions; type-conformance break at file tail/head; aggregate keywords.
- Options: (a) strip fully-blank leading/trailing rows automatically, flag suspected preamble/footer rows for confirmation; (b) row-error everything non-conforming.
- Default: (a).
- Class: `fix` for blank rows; `ask` for suspected preamble/footers.

**STR-07 · Line-ending inconsistency**
- What: Mixed CRLF/LF/CR; stray CR embedded at field ends (`value\r`).
- Detect: Line-terminator scan.
- Options: (a) normalize; (b) fail.
- Default: (a) normalize, counted.
- Class: `fix`.

---

## NUL — Null & Empty Semantics

**NUL-01 · Empty string vs. null**
- What: `,,` vs `,"",` — is an empty field null, or a legitimate empty string? Downstream systems treat these differently.
- Detect: Presence of empty fields; quoted-empty vs unquoted-empty distinction if dialect preserves it.
- Options: (a) empty → null for all columns; (b) empty → null except explicitly string-typed columns; (c) preserve distinction where the format allows.
- Default: (a) for non-string target types; for string targets, `ask` once at dataset level.
- Class: `ask` (string columns) / `fix` (typed columns).

**NUL-02 · Whitespace-only values**
- What: `"   "` — visually empty, technically present.
- Detect: Whitespace-only value count per column.
- Options: (a) trim, then apply NUL-01 rule; (b) preserve.
- Default: (a), counted per column.
- Class: `fix`.

**NUL-03 · Sentinel values**
- What: In-band null encodings: `N/A`, `NA`, `NULL`, `null`, `-`, `--`, `.`, `?`, `none`, `9999`, `-1`, `1900-01-01`, `00000000`.
- Detect: Known-sentinel dictionary hits per column; suspicious constant frequency spikes; type-violating constants in otherwise-typed columns.
- Options: per column: (a) confirmed sentinel list → null; (b) keep as data; (c) row-error.
- Default: **always ask** — proposed sentinel list is presented with counts, user confirms per column. `9999` may be a real quantity; `-1` may be a real offset. Never defaulted.
- Class: `ask`.

**NUL-04 · Null in non-nullable target**
- What: Null (after NUL-01/02/03 resolution) arriving at a column the destination declares NOT NULL.
- Detect: Sample nulls vs. destination schema; runtime check regardless.
- Options: (a) row-error; (b) substitute declared default value; (c) run-error.
- Default: (a) quarantine. Default-substitution only by explicit choice, and the substituted count is reported.
- Class: `row-error`.

**NUL-05 · Null propagation through transforms**
- What: What does `concat(first, ' ', last)` produce when `last` is null? What does `amount * rate` produce? Inconsistent answers are a classic silent-corruption source.
- Detect: N/A — this is a policy decision, always in effect.
- Options: (a) SQL semantics — any null operand yields null; (b) null-as-empty/zero per transform; (c) per-column override.
- Default: (a) SQL semantics, stated explicitly in the spec; overrides are per-mapping and visible.
- Class: policy (declared once).

---

## TYP — Type & Format Coercion

**TYP-01 · Formatted numerics**
- What: `1,234.56`, `$1,234`, `(500)` for -500, `12%`, trailing minus `500-`.
- Detect: Pattern classes per numeric-candidate column.
- Options: per detected pattern: (a) parse with named cleaning rule (thousands-sep, currency-strip, accounting-negative, percent→fraction); (b) strict parse (pattern is row-error).
- Default: propose (a) with the specific detected rules, confirm; unconfirmed patterns at runtime are row-errors.
- Class: `ask` at spec time; `row-error` at runtime.

**TYP-02 · Decimal/grouping locale**
- What: `1.234,56` (European) vs `1,234.56` (US) — same characters, opposite meaning.
- Detect: Separator-order analysis across column; ambiguous when all values have ≤1 separator.
- Options: declare locale per column: US-style / EU-style.
- Default: **always ask** when both interpretations are viable. Never guess.
- Class: `ask`.

**TYP-03 · Date format ambiguity**
- What: `01/02/2026` — Jan 2 or Feb 1. The canonical silent-corruption bug.
- Detect: Cross-row evidence (any day>12 disambiguates); absence of evidence = ambiguous; multiple formats coexisting in one column.
- Options: (a) declare explicit format per column; (b) multi-format columns: ordered format list with quarantine for non-conforming; (c) reject column until declared.
- Default: **always ask** when ambiguous — evidence-based inference is shown ("all 240 values parse as DMY; 31 fail as MDY") but the user confirms. Non-conforming values at runtime are row-errors.
- Class: `ask`; `row-error` at runtime.

**TYP-04 · Timezone semantics**
- What: Naive datetimes with unstated zone; mixed offsets in one column; DST-ambiguous local times.
- Detect: Offset presence/absence/mixture per datetime column.
- Options: (a) declare source zone for naive values, convert to UTC; (b) declare source zone, keep local; (c) offsets present: normalize to UTC.
- Default: naive datetimes **always ask** for source zone; storage convention (UTC vs local) is a dataset-level policy, default UTC.
- Class: `ask`.

**TYP-05 · Datetime→string rendering consistency**
- What: The same datetime rendered five ways across a pipeline (`2026-07-17`, `07/17/2026`, `17-Jul-26`…), breaking downstream parsing and joins.
- Detect: N/A — output policy.
- Options: single canonical rendering defined once at dataset level (default ISO-8601), per-mapping override with explicit format string.
- Default: ISO-8601 (`YYYY-MM-DD` / `YYYY-MM-DDTHH:MM:SSZ`). Overrides are visible in the spec.
- Class: policy (declared once).

**TYP-06 · Boolean representations**
- What: `Y/N`, `yes/no`, `0/1`, `TRUE/True/true`, `T/F`, checkbox artifacts (`X`/blank).
- Detect: Low-cardinality columns whose value set ⊆ known boolean vocabularies.
- Options: (a) map recognized vocabulary → boolean, confirm mapping; (b) keep as string; unmapped values are row-errors.
- Default: propose (a) with the detected vocabulary shown; confirm truth-mapping explicitly (does `X`/blank mean true/false or true/null?).
- Class: `ask`; `row-error` at runtime for out-of-vocabulary values.

**TYP-07 · Numeric-looking identifiers**
- What: ZIP codes, account numbers, EANs read as numbers: leading zeros destroyed, large IDs mangled by float precision, `1e5` scientific notation artifacts.
- Detect: Uniform-width digit strings; leading-zero presence; identifier-like column names; values exceeding 2^53.
- Options: (a) string type; (b) integer type.
- Default: (a) string when any leading-zero or width-uniformity evidence exists — propose and confirm. This mode is the reason "it's numbers" is not the same as "it's numeric."
- Class: `ask`.

**TYP-08 · Precision, scale & money**
- What: Currency in binary floats (0.1+0.2 problems), scale overflow on decimal targets, silent rounding.
- Detect: Decimal-place distribution; column-name/currency-symbol hints; destination decimal(p,s) vs observed values.
- Options: (a) decimal/exact type for money-like columns; (b) float by explicit choice; overflow → row-error or run-error.
- Default: (a); values exceeding declared scale are row-errors (never silently rounded — rounding is an explicit transform).
- Class: `ask` for type; `row-error` at runtime.

**TYP-09 · Domain-range violations**
- What: Values that parse but can't be true: birthdates in 1802 or 2031, negative quantities, percentages of 250.
- Detect: Only when destination schema or user declares ranges; profiler flags statistical outliers as candidates.
- Options: per column: (a) no constraint; (b) declared range, violations are row-errors; (c) declared range, violations are warnings.
- Default: (a) unless the destination schema declares constraints, in which case (b).
- Class: `row-error` when constrained.

**TYP-10 · Case & whitespace in comparable values**
- What: ` Gold` vs `gold` vs `GOLD` as category values or join keys; trailing-space mismatches.
- Detect: Value clusters that collide under trim+casefold in low-cardinality or key columns.
- Options: (a) trim + canonical-case for matching/enum columns (canonical form chosen per column); (b) exact preservation.
- Default: trim always for enum/key comparison (`fix`, counted); case canonicalization proposed with the detected collision clusters shown.
- Class: `fix` (trim) / `ask` (case).

**TYP-11 · Length overflow / truncation**
- What: Value exceeds destination column length. Silent DB truncation is data loss with no witness.
- Detect: Sample max-length vs destination schema length.
- Options: (a) row-error; (b) explicit truncate-with-count; (c) run-error.
- Default: (a) quarantine. Truncation only as an explicit, counted choice.
- Class: `row-error`.

**TYP-12 · Magnitude/scale-suffixed numerics**
- What: Numbers carrying a magnitude suffix — `10.00K`, `1.2M`, `3B` — where the letter is a scale factor, not text. Left unhandled, the column won't parse as numeric; guessed wrong (K=1000 vs K=1024, or a real trailing letter that isn't a scale) it silently mis-scales every value. Observed in NOAA Storm Events damage fields (`corpus/`).
- Detect: Column values matching `<number><K|M|B|G|T>` (optionally currency-prefixed) above a frequency threshold.
- Options: (a) parse with a confirmed suffix→exponent map (decimal SI: K=10³, M=10⁶, B/G=10⁹, T=10¹²); (b) keep as string; (c) row-error on the suffix.
- Default: **always ask** — propose (a) with the detected suffixes and the SI interpretation shown, confirm. The suffix set and their meaning are declared in the spec; unconfirmed suffixes at runtime are row-errors. Never applied by default: a trailing letter may be a unit, a grade, or a typo, and mis-scaling is silent corruption.
- Class: `ask` at spec time; `row-error` at runtime for unmapped suffixes.

---

## KEY — Keys, Cardinality & Shape

**KEY-01 · Duplicate keys**
- What: Multiple rows share a declared primary/business key.
- Detect: Key-uniqueness check on sample when key is declared.
- Options: (a) run-error; (b) quarantine all rows of duplicated keys; (c) first-wins / last-wins with counted drops.
- Default: (a) for primary keys — duplicate keys usually mean the extract is wrong, not the rows. First/last-wins only by explicit choice with ordering declared.
- Class: `run-error` by default.

**KEY-02 · Missing expected columns**
- What: A column the spec maps from is absent at runtime.
- Detect: Runtime header validation (always on).
- Options: (a) run-error; (b) proceed with column as all-null if mapping declares it optional.
- Default: (a). Optionality is per-mapping and explicit.
- Class: `run-error`.

**KEY-03 · Unexpected new columns**
- What: Input contains columns the spec doesn't mention (schema drift).
- Detect: Runtime header validation.
- Options: (a) ignore + warn; (b) run-error (strict shape).
- Default: (a) warn — but the warning is prominent in the run summary because drift often precedes breakage.
- Class: warning by default.

---

## ERR — Error Handling, Reporting & Run Semantics

These are run-level policies, always present in every spec.

**ERR-01 · Failure disposition**
- What: What happens when a row hits any `row-error` mode.
- Options: (a) fail-fast — first row-error aborts the run; (b) quarantine — bad rows diverted, run continues; (c) annotate — bad rows pass through with error columns appended, nulls in failed fields.
- Default: (b) quarantine. The quarantine file preserves the **original raw row** plus error records (see ERR-03) so no information is lost and reprocessing is possible.

**ERR-02 · Error budget**
- What: Row-level tolerance masking systemic failure — 60% of rows quarantining isn't dirty data, it's a wrong spec or wrong file.
- Options: threshold as % or absolute count; exceeding it converts the run to failure.
- Default: run fails if >5% of rows (min 100) quarantine. Threshold recorded in spec.

**ERR-03 · Reporting granularity**
- What: Per-record vs per-run; by row vs by error type — the fidelity question.
- Options: any subset of: (i) per-row error records `(row_number, column, error_code=taxonomy ID, offending_value, message)`; (ii) per-error-type aggregates with per-column counts; (iii) run summary (rows in/out/quarantined/warned, all auto-fix counts, applied-defaults list).
- Default: **all three, always.** They serve different consumers (debugging, monitoring, auditing) and each is cheap once the others exist. Machine-readable (JSONL/JSON) + human-readable summary.

**ERR-04 · Warning semantics**
- What: The boundary between "fixed it, tell me" and "couldn't fix it, row affected."
- Policy: every `fix`-class action is a **warning**, counted per column per taxonomy ID, never dropped from the summary. Warnings never quarantine rows. A warning volume spike is surfaced in the run summary as an anomaly signal.

**ERR-05 · Output atomicity**
- What: Partial output from an aborted run poisoning downstream consumers.
- Policy: output is written to a temporary location and atomically promoted on success; a failed run leaves no partial output, only the error report. Always on; not configurable in v1.

**ERR-06 · Run provenance**
- What: "Which code, which spec, which input produced this file?"
- Policy: every run emits a manifest: spec version + hash, taxonomy version, generator version, input file hash + row count, output row count, quarantine count, timestamp, effective policy table (including defaults applied). Always on.

---

## How the Pieces Consume This Document

| Component | Consumes | Produces |
|---|---|---|
| Profiler | Detection signals per entry | Findings JSON keyed by taxonomy ID, with evidence and counts |
| Interview | `ask`-class findings + unresolved defaults | Decisions with provenance |
| Spec | Every entry's decision (explicit/default/confirmed) | The complete, auditable policy record |
| Runtime library | Options semantics, one implementation per ID | Uniform behavior + error records coded by ID |
| Run report | ERR-03/04 policies | Per-row errors, per-type aggregates, run summary, manifest |

## Validation Plan (before building anything else)

1. Collect five genuinely messy real-world files (different domains, at least one vendor export, at least one Excel-exported CSV).
2. For each, list everything actually wrong with it — by hand, without reference to this document.
3. Check every observed problem maps to a taxonomy ID. Anything unmapped becomes a new entry; anything chronically unused is a candidate to cut.
4. Sanity-check defaults: for each file, would the defaults + `ask`-class questions have produced the pipeline you'd have written by hand? Where not, the default is wrong or the decision space is missing an option.

## Explicitly Out of Scope (v0.1)

Multi-table joins and referential integrity across files; nested/semi-structured data; streaming semantics; incremental/CDC loads; PII detection and masking (a natural future category — `PRV` — but a serious one that deserves its own design pass, not a bolt-on).
