# The .etlspec.yaml Format (v0.3)

The spec is the single source of truth for a pipeline: every mapping, every policy decision, with provenance. It must be complete enough to regenerate the pipeline without re-profiling or re-interviewing.

**v0.2 additions** (backward compatible; the compiler accepts 0.1–0.3): the optional
top-level `skip_rows:` section (STR-06 confirmed row exclusions, declarative), and two new
transform ops — `repair_mojibake` (ENC-06, opt-in, report-counted) and `concat` (NUL-05
SQL null propagation).

**v0.3 addition — the `annotate` disposition (ERR-01 option c, evidence-backed).**
`error_disposition` accepts `quarantine` (house default) | `fail-fast` | `annotate`.
Under `annotate` (requires runtime ≥ 0.4.0), a **content** failure NULLs the failed field,
the row **loads**, and a per-row ledger entry `{field, change: NULLED, reason: <taxonomy
id>}` is written to `changes.jsonl` (shape adopted from Airbyte's `_airbyte_meta.changes`;
reasons are taxonomy IDs) and aggregated in `summary.annotations_by_type`. Two failures
never annotate: **structural** rows (STR-02 — no single field to repair) and **NUL-04**
(a declared `nullable: false` target cannot be repaired by nulling) — both quarantine as
usual. Annotated runs exit 2 ("investigate"), same as quarantining runs. An unknown
disposition value is a compile-time SpecError and a runtime run-error — never a silent
fallback. Example `skip_rows`:

```yaml
skip_rows:                        # STR-06: confirmed non-data rows, matched on the RAW value
  - {column: order_id, pattern: '^\D', id: STR-06, reason: "footer/total row", provenance: detected-confirmed}
```

A complete worked v0.2 example (all 17 trap classes of the demo file): `evals/inputs/orders_export.etlspec.yaml` in the repo.

## Provenance values

Every decision carries `provenance`:
- `explicit` — the user chose it in the interview or by editing
- `detected-confirmed` — the profiler proposed it from evidence and the user confirmed
- `default` — the taxonomy house default applied without discussion (nothing was detected requiring a question)
- `unconfirmed` — chosen in unattended mode without user confirmation; must be surfaced for review

## Top-level structure

```yaml
etlspec: 0.1                     # spec format version
name: vendor_orders               # pipeline name (snake_case)
taxonomy_version: 0.1             # taxonomy this spec was authored against

source:
  format: csv
  encoding: {value: utf-8, provenance: detected-confirmed}   # ENC-01
  dialect:
    delimiter: ","                # STR-01
    quotechar: '"'
  header: present                 # STR-04: present | absent (names below supply order)
  expected_columns: [order_id, cust_name, amt, dt, active, zip]   # KEY-02/03 validation list

target:
  columns:                        # ordered; drives output column order
    - {name: order_id, type: string, nullable: false}
    - {name: customer_name, type: string, nullable: false}
    - {name: amount, type: decimal, scale: 2, nullable: true}
    - {name: order_date, type: date, nullable: true}
    - {name: is_active, type: boolean, nullable: true}
    - {name: postal_code, type: string, nullable: true, max_length: 10}

policies:                         # dataset-level; taxonomy IDs in comments
  unicode_normalization: {value: NFC, provenance: default}          # ENC-03
  strip_control_chars: {value: true, provenance: default}           # ENC-04
  normalize_unicode_whitespace: {value: true, provenance: default}  # ENC-05
  trim_whitespace: {value: true, provenance: default}               # NUL-02
  empty_string_is_null: {value: true, provenance: explicit}         # NUL-01
  null_propagation: {value: sql, provenance: default}               # NUL-05
  datetime_rendering: {value: iso8601, provenance: default}         # TYP-05
  error_disposition: {value: quarantine, provenance: default}       # ERR-01
  error_budget: {value: {percent: 5, min_rows: 100}, provenance: default}  # ERR-02
  duplicate_rows: {value: keep, provenance: default}                # STR-05: keep | drop_exact

mappings:                         # one entry per target column
  - target: order_id
    source: order_id
    transforms: []                # clean text + not-null enforced by target nullability
    decisions:
      - {id: TYP-07, choice: string, provenance: detected-confirmed,
         evidence: "uniform 8-digit values"}
  - target: customer_name
    source: cust_name
    transforms: []
  - target: amount
    source: amt
    transforms:
      - {op: to_decimal, thousands_sep: ",", currency: true, accounting_negative: true}
    decisions:
      - {id: TYP-01, choice: "strip $ and commas; (n) = negative",
         provenance: detected-confirmed, evidence: "$1,234.56 pattern in 41 rows; (500) in 3"}
  - target: order_date
    source: dt
    transforms:
      - {op: to_date, formats: ["%m/%d/%Y"]}
    decisions:
      - {id: TYP-03, choice: MDY, provenance: explicit,
         evidence: "ambiguous in sample; user confirmed US-format source system"}
    sentinels: {values: ["N/A"], provenance: detected-confirmed}     # NUL-03
  - target: is_active
    source: active
    transforms:
      - {op: to_bool, mapping: {Y: true, N: false}}
    decisions:
      - {id: TYP-06, choice: "Y/N vocabulary", provenance: detected-confirmed}
  - target: postal_code
    source: zip
    transforms: []
    decisions:
      - {id: TYP-07, choice: string, provenance: detected-confirmed,
         evidence: "leading zeros present (02134)"}

unmapped:
  unused_source_columns: []       # listed explicitly, never silently dropped
  unfilled_target_columns: []

review_required: []               # IDs of unconfirmed decisions (unattended mode); empty when fully confirmed
```

## Rules

1. **Completeness:** every policy key above appears in every spec, even when defaulted. A reader must never wonder "what does this pipeline do about X?"
2. **Sentinels are per-column** (NUL-03) and always carry evidence. Never spec a dataset-wide sentinel list.
3. **Transforms are ops from the runtime's vocabulary** (`to_decimal`, `to_date`, `to_datetime`, `to_bool`, `to_int`, `repair_mojibake`, `concat` `{sources, sep}`, `constant`, `format_datetime`, custom expressions as `{op: expr, python: "..."}` — expressions may reference `row` and `report`, and are a visible escape hatch; use sparingly). `split` is declared in the vocabulary but the compiler currently declines it (its miss-semantics have no taxonomy home yet — use `expr`).
4. **Order matters:** transforms apply left to right, after text cleaning and null resolution (which the runtime applies per policy before any transform runs).
5. **A null reaching a `nullable: false` target column is NUL-04** — row-error, handled by the runtime; do not add explicit checks per mapping.
