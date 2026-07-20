# vendor_orders pipeline — regeneration notes

Regenerated Python pipeline from `/home/claude/work/eval-inputs/vendor_orders.etlspec.yaml`
(etlspec 0.1). No decisions in the spec were changed; every policy and mapping
decision (TYP-01, TYP-03, TYP-06, TYP-07) is encoded as-is in the generated code.

## Files

- `vendor_orders_pipeline.py` — the generated pipeline. Run with:
  `python3 vendor_orders_pipeline.py INPUT_CSV OUTPUT_CSV [--quarantine Q.csv] [--report R.json]`
- `vendor_orders_output.csv` — transformed output from the sample run (4/4 rows written).
- `vendor_orders_quarantine.csv` — quarantined rows from the sample run (empty; header only).
- `vendor_orders_run_report.json` — run report (row counts, error budget, status).

## Verification run

Input: `/home/claude/work/eval-inputs/vendor_orders_sample.csv`
Result: exit code 0, status `success` — 4 rows read, 4 written, 0 quarantined
(error budget allowed: 2 = max(floor(25% of 4), min_rows 2)).

Spot checks against the spec's decisions:

| Source value | Output | Spec decision |
|---|---|---|
| `"$2,450.00"` | `2450.00` | TYP-01: strip `$` and commas |
| `(75)` | `-75.00` | TYP-01: accounting negative |
| `"$1,000"` | `1000.00` | TYP-01 + decimal scale 2 |
| `01/15/2026` | `2026-01-15` | TYP-03: MDY, rendered ISO-8601 |
| `N/A` (dt) | NULL (empty field) | sentinel `N/A` on order_date |
| `Y` / `N` | `true` / `false` | TYP-06: Y/N vocabulary |
| `02134` | `02134` | TYP-07: zip as string (leading zeros kept) |

Edge-case unit checks were also run (accounting negatives with currency,
scale-2 HALF_UP rounding, invalid date/bool rejection, control-char stripping,
unicode whitespace normalization, empty-string-to-NULL, multi-field error
messages for quarantine) — all passed.

## Interpretation notes (implementation details, not spec changes)

- Error budget `{percent: 25, min_rows: 2}` is implemented as: pipeline fails
  (exit 1) if quarantined rows exceed `max(floor(rows_read * 25%), 2)`.
- NULLs are rendered as empty CSV fields; booleans as `true`/`false`; decimals
  with fixed scale 2.
- `error_disposition: quarantine` writes failing source rows (with row number
  and reason) to the quarantine CSV instead of aborting the run.
- Header is validated against the spec's `expected_columns`; a mismatch is a
  fatal error (exit 1) since the spec records no unused/unfilled columns.
