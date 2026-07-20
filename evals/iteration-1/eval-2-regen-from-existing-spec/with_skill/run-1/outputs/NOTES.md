# vendor_orders — pipeline regeneration notes

Date: 2026-07-17
Skill: etl-generator (taxonomy v0.1, runtime v0.1.0)

## What was done

The mapping spec already existed at `/home/claude/work/eval-inputs/vendor_orders.etlspec.yaml`
(workflow steps 1–5 complete), so this run executed only:

- **Step 6 — Generate:** copied `etl_runtime.py` verbatim from the skill's `assets/`
  (unmodified, per the skill contract) and generated `vendor_orders_pipeline.py`, a thin
  orchestration script that embeds the resolved spec as `CONFIG` and delegates all edge-case
  semantics to the runtime. Every mapping line carries a comment tracing it to the spec's
  taxonomy decisions and provenance. **No decisions recorded in the spec were changed.**
- **Step 7 — Verify:** ran the pipeline against
  `/home/claude/work/eval-inputs/vendor_orders_sample.csv`.

## Spec decisions carried into the code (unchanged)

| Target | Source | Decision |
|---|---|---|
| order_id | order_id | TYP-07 string (detected-confirmed); non-nullable → NUL-04 check |
| customer_name | cust_name | direct rename; non-nullable → NUL-04 check |
| amount | amt | TYP-01 strip $/commas, (n)=negative (detected-confirmed); decimal scale 2, TYP-08 guard |
| order_date | dt | TYP-03 MDY `%m/%d/%Y` (explicit — user-confirmed US source); sentinel "N/A" → null (NUL-03) |
| is_active | active | TYP-06 Y/N vocabulary (detected-confirmed) |
| postal_code | zip | TYP-07 string, leading zeros preserved (detected-confirmed); TYP-11 max_length 10 |

Policies mirrored into `CONFIG["policies"]`: NFC normalization, control-char strip, unicode
whitespace normalization, trim, empty-string-is-null (explicit), sql null propagation,
iso8601 rendering, quarantine disposition, error budget {percent: 25, min_rows: 2}
(explicit), duplicate rows kept.

## Run result

Command:
`python vendor_orders_pipeline.py /home/claude/work/eval-inputs/vendor_orders_sample.csv --out-dir ./etl_out`

**Exit code 0 (clean run).**

- rows_in: 4, rows_out: 4, rows_quarantined: 0, row errors: 0
- Warnings (counted auto-fixes, per ERR-04): `NUL-03:dt = 1` — the one "N/A" value in `dt`
  (row 3, order 20000003) resolved to null `order_date`, exactly as the spec's
  detected-confirmed sentinel decision dictates. This matches profiling expectations.
- Spot checks against the sample:
  - `$2,450.00` → `2450.00`; `(75)` → `-75.00` (accounting negative); `$1,000` → `1000.00`
  - `01/15/2026` → `2026-01-15` (MDY, ISO rendering)
  - `Y`/`N` → `true`/`false`
  - `02134` kept as string with leading zero
- No `quarantine.csv` was produced because zero rows were quarantined (the runtime only
  writes it when rows are quarantined).

## Files in this outputs directory

- `vendor_orders_pipeline.py` — generated pipeline (regenerated from the unchanged spec)
- `etl_runtime.py` — runtime module, verbatim copy from the skill's assets
- `etl_out/output.csv` — transformed output (4 rows)
- `etl_out/summary.json` — run summary (ERR-03)
- `etl_out/errors.jsonl` — per-row errors (empty: no row errors)
- `etl_out/manifest.json` — run manifest with input sha256 and effective policies (ERR-06)
