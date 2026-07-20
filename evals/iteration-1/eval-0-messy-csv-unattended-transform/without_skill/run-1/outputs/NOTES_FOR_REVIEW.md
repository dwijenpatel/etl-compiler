# Vendor orders ETL — notes for your review

Run date: 2026-07-17. Pipeline succeeded (exit 0): 9 data lines read, 6 rows
written, 3 rows dropped (all logged with reasons), 0 rows quarantined.

## What's here

| File | What it is |
|---|---|
| `transform_orders.py` | The ETL script. `python3 transform_orders.py INPUT OUTPUT [--report R.json]` |
| `orders_warehouse.csv` | Transformed output from `/home/claude/work/eval-inputs/orders_export.csv` |
| `orders_warehouse.report.json` | Run report: row counts, every dropped/quarantined row with line number and reason |

Verified: the header + first 3 data rows of the output are **byte-identical**
to the sample at `/home/claude/work/eval-inputs/target_orders.csv` (including
Unicode form and line endings).

## Transformations applied (as implied by the target sample)

- Columns renamed: `customer`→`customer_name`, `amt`→`amount`,
  `active`→`is_active`, `zip`→`postal_code`; stray spaces in vendor headers stripped.
- `amount`: `"$1,234.56"`→`1234.56`; accounting parens `(500)`→`-500.00`
  (negative — confirmed by the target sample); always 2 decimals, Decimal math.
- `order_date`: `MM/DD/YYYY`→`YYYY-MM-DD`.
- `is_active`: `Y`/`N`→`true`/`false` (also accepts yes/no/1/0/true/false).
- `postal_code`: kept as text so leading zeros survive (`02134` stays `02134`);
  plain-numeric zips shorter than 5 digits are zero-padded.
- `notes`: `N/A` placeholders and whitespace-only values become empty; the row
  that was short one field (line 7, Carol — vendor omitted the notes column
  entirely) gets an empty note.
- Text cleanup: UTF-8 BOM and mixed `\r\n`/`\n` line endings handled; output is
  BOM-less UTF-8 with `\n` endings and NFC-normalized accents (the vendor file
  mixes composed and decomposed forms — target uses composed).

## Judgment calls to review

1. **Date order assumption.** Vendor dates are read as US **MM/DD**/YYYY. The
   target sample confirms this (`01/02/2026` → `2026-01-02`), but every date in
   this file has day ≤ 12, so DD/MM data would be silently misread. If this
   vendor is non-US, flip the format list in `parse_date()`.
2. **"Bob Smith" (order 10000005).** The raw field is `Bob\x0bSmith` — a
   vertical-tab control character between the names. I treated it as a space →
   `Bob Smith`. If it's actually one word (`BobSmith`), edit `clean_text()` to
   delete control chars instead of collapsing them to a space. Same rule turned
   the non-breaking space in `Ann\xa0Lee` into a regular space.
3. **Duplicate dropped.** Order 10000005 appeared twice, byte-for-byte
   identical after cleaning (lines 6 and 8). I kept the first and dropped the
   second (logged in the report). If the same `order_id` ever reappears with
   *different* data, the script keeps both rows and prints a warning instead of
   guessing.
4. **Mojibake repair.** Line 4 was double-encoded (`JosÃ© GarcÃ­a`); repaired
   to `José García`, matching the target sample. The repair only triggers on
   clear mojibake byte patterns, so correctly-encoded names are never touched.
5. **Vendor "Total" footer doesn't reconcile — and that's expected.** The
   footer says 3222.56; the clean output sums to **2147.56**. Difference
   exactly explained: the vendor total counts the duplicate row (+75.00) and
   treats `(500)` as +500 instead of −500 (+1000.00). 2147.56 + 75 + 1000 =
   3222.56. Good evidence the dedupe and sign handling are right.
6. **Dropped rows** (all in the JSON report): the exact duplicate (line 8), one
   blank line (line 9), and the `Total,,3222.56,,,,` footer (line 10).

## For future runs

- Exit codes: `0` success, `1` fatal (missing columns / empty input), `2` ran
  but some rows failed validation. Bad rows are never silently dropped — they
  go to `quarantined_rows` in the report with line number, reason, and raw text.
- The script validates the header up front and fails fast if the vendor changes
  their column set.
