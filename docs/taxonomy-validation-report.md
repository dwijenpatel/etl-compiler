# Taxonomy Validation Report

**Date:** 2026-07-20
**Taxonomy under test:** v0.1 → v0.2 (this pass added TYP-12)
**Method:** three independent evidence streams, deliberately non-circular (the v0.1
taxonomy and its synthetic traps were authored from the same priors, so testing against
them proves nothing about coverage).

| Stream | What | Where |
|---|---|---|
| **A — corpus audit** | Profiler swept across 244 real tabular files: 214 parser-test fixtures (csvkit/frictionless/PapaParse) + 30 genuine open-data portal files (US/UK/FR/DE/JP). | `corpus/` (reproducible via `manifest.json` + `fetch_corpus.py`); audit harness `corpus/audit.py` |
| **B — literature/incidents** | Academic dirty-data taxonomies + empirical frequency studies + practitioner catalogs + documented incidents. | `~/repos/etl-evidence/external/{literature,practitioner-evidence}` |
| **D — tool census** | How 15 shipping tools (parsers, EL frameworks, DQ tools, enterprise loaders) name and default their failure handling. | `~/repos/etl-evidence/external/{csv-engines,el-frameworks,dq-tools,enterprise-etl}` |

Evidence weighting follows the evidence-kit grading method: streams B/D are **retrieval
grade** (mapped, mirrored, tagged — nothing is Tier-A/load-bearing yet); the live-executed
tool behaviors are A3-eligible for a later adversarial pass. **No house default was
changed on this evidence.** Only additions (minor version) and profiler/runtime fixes were
applied; every default-change candidate is listed as an open proposal.

---

## Headline: the taxonomy holds up; the *profiler* was the weak link

The v0.1 taxonomy's **categories** survived contact with real data well — every problem
found across 244 files mapped to an existing family, with two genuine coverage gaps (one
now closed as TYP-12, one deferred). What did **not** hold up was *detection*: the profiler
silently missed several modes the taxonomy already names, which is the more dangerous
failure because detection-driven elicitation means an undetected `ask`-class mode is never
surfaced — the exact silent-decision the project exists to prevent.

External frequency evidence independently confirms the problem class is real and common,
not synthetic:

- **~26% of open-government CSVs fail naive parsing** (Mitlöhner et al. 2016, N≈142k,
  independently confirmed) — mirror: `etl-evidence/.../literature`.
- **20.2% of GitHub CSVs use a non-standard dialect** vs 0.62% of gov CSVs (CleverCSV
  2019) — messiness is provenance-dependent, ~30×.
- **~8% of open CSVs are semicolon-delimited** (comma-decimal locales) — independently
  confirms STR-01 + TYP-02 are high-frequency, not edge. Every FR/DE portal file in our
  corpus was semicolon-delimited.
- **19.6%→30.9% of genomics papers** carried Excel-autocorrupted gene names over 2016→2021
  — a single tool's silent coercion, unabated after publicity. The canonical
  silent-corruption case the taxonomy's `ask` class targets.

---

## Stream A findings — what the corpus taught

### Profiler bugs found and fixed (all now pinned by `tests/test_profiler.py`)

| Signal | File(s) that exposed it | Fix |
|---|---|---|
| **Crash on empty CSV** | `frictionless/data__empty.csv` | `profile_structure` returned `[], []` (list, not dict); guard added. |
| **TYP-06 over-fire** | `frictionless/data__wide.csv` (9,074 constant columns → 9,073 spurious boolean findings) | Require ≥2 distinct values (or the X/blank checkbox pattern) before flagging a boolean vocabulary. |
| **STR-06 preamble undetected** | 3 ONS time-series files (8 metadata rows read *as the header*) | `detect_preamble`: width-break + type-stabilization signals; ONS files now flag 8 preamble rows for confirmation. |
| **TYP-03 dotted dates missed** | all DE/FR files (`08.08.2018`) | date regex now accepts `.` separator. |
| **NUL-03 sentinels missed** | NYPD (`(null)`), CDC (`Missing`) | dictionary extended: `(null)`, `(none)`, `n.a.`, `not available`, etc. |
| **ENC-02 doubled BOM** | Leicester licensing (two UTF-8 BOMs; one survived into the first header name) | strip *every* leading BOM, in both profiler and runtime. |

Post-fix audit: **244 files, 0 crashes, 75 silent — and all 75 silent files are
intentionally-clean parser fixtures; zero real portal files were silent.** No whole-file
coverage blindness on genuine data.

ID frequency across the corpus (files fired in): STR-01 60 · NUL-01 58 · TYP-07 30 ·
STR-06 26 · TYP-06 25 · STR-02 21 · STR-05 19 · STR-04 18 · NUL-03 14 · ENC-01 13 · TYP-02
11 · TYP-03 9 · ENC-02 8 · TYP-01 8 · TYP-12 (new) fires on NOAA. **No taxonomy ID went
entirely unused** across the corpus — no evidence for cutting any entry.

### Detection-quality gaps still open (not silently left — logged here)

- **ENC-01 can't distinguish multibyte encodings.** The Latin-1 fallback *always* decodes
  (Latin-1 maps every byte), so genuine Shift-JIS (JP JMA files) is flagged ENC-01 but
  mislabeled `latin-1` with mojibake column names. A real charset-detection step (byte
  n-gram / `chardet`-style) is needed to name the encoding. Stdlib-only constraint makes
  this non-trivial — **proposed**, not done.
- **Preamble detection is scoped to 2-column key-value files** (the ONS shape) plus the
  width-break case. A wide table with same-width preamble rows would still be missed. The
  restriction is deliberate (avoids mistaking a formatted-numeric column for a type break —
  it regressed the smoke sample before I scoped it), and noted as a known limit.

---

## Coverage: mapping real problems to taxonomy IDs

Every distinct problem observed across streams A/B/D mapped to a taxonomy family except the
following. Assessed against the taxonomy's own bar: *finite, enumerable decision space*.

### Closed this pass

- **TYP-12 · Magnitude/scale-suffixed numerics** (`10.00K`, `1.2M`). Evidence: NOAA Storm
  Events damage fields (672 K-suffixed values in one file). Finite decision space (SI
  suffix→exponent map, or keep-string, or row-error). **Added** to the taxonomy (v0.2),
  detected by the profiler, implemented in the runtime (`to_decimal(magnitude=True)`),
  tested. Default: **always ask** — a trailing letter may be a unit or a typo, and
  mis-scaling is silent corruption.

### Deferred — real, but decision space not yet finite (proposals, not additions)

- **Embedded structured data in a cell** (GeoJSON, WKT polygons, HTML `<br/>`). Evidence:
  Toulouse/Nantes portal files; CSVW-UCR names it ("cell microsyntax"). The decision is
  keep-as-string vs *extract* — and extraction is nested-data parsing, which the taxonomy
  explicitly scopes out (v0.1 § Out of Scope). Recommend a small `ask`/flag entry ("cell
  contains embedded markup/JSON — keep verbatim?") rather than an extraction transform.
- **Misfielded / shifted values** (a value in the wrong column; several values crammed into
  one field). Evidence: Rahm-Do 2000 lists both as core instance problems. Overlaps STR-03
  (quoting) at runtime but the *detection* signal (a type that belongs in a neighbor
  column) is different. Decision space unclear — **proposed** for a design pass.
- **PII / `PRV`** — already a deferred category in v0.1. External precedent is now on
  record: Deequ ships 4 semantic PII checks (`containsCreditCardNumber`, …) as first-class.
  Confirms `PRV` is a real, nameable family when its design pass comes; no action now.

Rival taxonomies diffed against ours (GE's 8-category issue enum, Rahm-Do's 2×2, the 46
"Falsehoods about CSVs", CSVW-UCR's 25 use cases) surfaced **no additional gap** beyond the
above — a reassuring coverage signal, since three of those four were authored with no
knowledge of our taxonomy (anchoring-free).

---

## The one default worth reconsidering: ERR-01 (quarantine) — RESOLVED, keep it

> **Update 2026-07-20: the adversarial pass ran.** Verdict: **keep the quarantine default.**
> Full holdings + distilled Tier-A rows in `~/repos/etl-evidence/external/error-disposition-defaults/`.
> The content-vs-movement crux resolved — fail-loud defaults *do* fire on content, so the
> conflict is real, but tools split into **four** default poles (fail-loud / annotate-and-keep
> / silent-coerce / quarantine), none of them quarantine. Decisive points: (1) every
> non-quarantine default that keeps the row produces the "loaded fine but silently degraded"
> artifact this project exists to prevent; (2) the most-used parsers (pandas/DuckDB/pyarrow)
> **silently coerce** content errors by default (live-verified) — quarantine is strictly
> better, not in conflict, there; (3) ERR-02's error budget already makes the behavior
> "quarantine up to a threshold, then fail loud," a hybrid; (4) the one documented directional
> switch (Airbyte V1→V2) moved *away* from fail-loud. Non-default follow-ups the evidence
> *does* support: keep ERR-01 option (c) "annotate" as first-class and adopt Airbyte's
> `_airbyte_meta {field,change,reason}` shape for it; import DuckDB's `reject_errors` schema
> as the ERR-03(i) reference; consider a future ADF-style `redirect` disposition. The
> original retrieval-grade finding is preserved below for the record.

**Finding (stream D, `A`-quality once adversarially confirmed):** across 11 surveyed tools,
**none defaults to row-quarantine-and-continue** — the v0.1 ERR-01 house default. The
shipped defaults cluster into four other dispositions:

| Disposition | Tools (default behavior) |
|---|---|
| **Fail-loud / abort** | Snowflake `ABORT_STATEMENT`, BigQuery `maxBadRecords=0`, SSIS Fail Component, Talend die-on-error, ADF skip-disabled, pandas `on_bad_lines='error'`, Arrow, Meltano SDK validate-then-halt |
| **Annotate-and-load** (write the row, mark the field) | Airbyte `_airbyte_meta.changes`, Fivetran type-lock (NULL + warning) |
| **Value-diversion** (route bad value to a sibling column) | dlt variant columns |
| **Report-and-continue** (all rows out, errors to a side report, exit 1) | csvkit `csvclean` |

**What this does and does not license.** It is an *unoccupied-niche* signal about the
default, warranted at Tier-A only about the surveyed sample (11 tools, 2026-07-20), Tier-B
about the world. The evidence-kit fit-check applies literally: **unoccupied ≠ wrong ≠
valuable.** There are two honest readings, and the corpus cannot yet adjudicate between them:

1. *The default is a genuine differentiator* — quarantine-with-raw-preservation +
   full accounting is exactly what the incident record (PHE cases vanished, gene names
   silently rewritten, CleverCSV's "parser returned without error" row-dropping) shows the
   world lacks. No surveyed tool does it *by default*, so it's the project's edge.
2. *The default fights user expectation* — every tool an engineer has used fails loud or
   loads-and-annotates; a quarantine default may surprise, and the enterprise loaders'
   near-unanimous fail-loud stance may encode real operational wisdom (a bad batch should
   stop the line, not silently divert 4% of rows).

**Recommendation:** do **not** change ERR-01 on this evidence. Run a targeted
adversarial-grade pass on this one question before touching it (a default change is a
major-version event per the project's own rule). The pass should specifically read whether
the fail-loud defaults are about *data-content* errors (our quarantine case) or
*data-movement* errors (Airbyte's distinction) — the tools may not actually disagree with
us about content rows once that axis is separated. Note also that ERR-02 (error budget)
already converts a high-quarantine run into a hard failure, so our default is closer to
"fail-loud past a threshold" than the table suggests.

Two shipped mechanisms are worth importing regardless of the default question:
- **DuckDB `reject_errors` table** — a columnar per-row error schema (`line`,
  `byte_position`, `column_name`, `error_type`, `csv_line`, `error_message`); prior art for
  our ERR-03(i) per-row records.
- **Airbyte `_airbyte_meta.changes`** — a machine-readable per-field change ledger
  (`{field, change, reason}` with a typed reason enum); prior art for how ERR-04 auto-fix
  accounting could be structured per-field rather than per-column.

---

## Interview scalability (stream A, structural)

The profiler emits `ask`-class findings **per column**. On a 9,074-column indicator matrix
that was 9,073 questions; on a routine 43-column NYC file it is dozens. An interview cannot
ask them one at a time. CLAUDE.md already suspected this for NUL-01; the corpus shows it is
**general** across every per-column `ask` family (TYP-06, TYP-07, NUL-01, NUL-03). The
SKILL.md interview step needs a **batching rule**: group homogeneous findings ("47 columns
look like Y/N booleans — confirm the mapping once for all") rather than iterating. This is a
skill/profiler change, not a taxonomy change — filed for the next skill iteration.

---

## What changed on disk this pass

- **Taxonomy v0.2** (both copies synced): added TYP-12; changelog + report pointer.
- **Runtime**: `to_decimal(magnitude=True)` (TYP-12); `TAXONOMY_VERSION` → 0.2.
- **Profiler**: 6 detection fixes above.
- **Tests**: `tests/test_profiler.py` (10 cases) + 2 new runtime cases; 55 total, green.
- **Corpus** (`corpus/`): reproducible fetcher, audit harness, 33-source manifest.
- **Evidence corpus** (`~/repos/etl-evidence`): 6 subtopics, retrieval-grade holdings.

## What did NOT change (deliberately)

- No house default altered. ERR-01 flagged for a dedicated adversarial pass.
- No taxonomy entry cut (all fired at least once across 244 files).
- Deferred coverage candidates (cell-embedded data, misfielded values, PRV) left as
  proposals — their decision spaces aren't finite enough to meet the taxonomy's own bar yet.

## Recommended next steps

1. **Adversarial-grade pass on ERR-01** (the only load-bearing default question raised).
2. **Interview batching** in SKILL.md/profiler (homogeneous `ask` findings grouped).
3. **Charset detection** for ENC-01 (name Shift-JIS et al., not just "not UTF-8").
4. **Then** rerun eval iteration-2 against the improved taxonomy/profiler (the 6 archived
   runs predate these changes and should be regenerated, not graded as-is).
