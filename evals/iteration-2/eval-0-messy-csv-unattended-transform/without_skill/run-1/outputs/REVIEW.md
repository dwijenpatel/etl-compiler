# Orders ETL — run notes & things to review

**What ran:** `etl_orders.py` transformed `eval-inputs/orders_export.csv` into the
warehouse format defined by the sample `eval-inputs/target_orders.csv`.

**Result:** 9 data rows in → **5 clean rows out** (`output/orders_warehouse.csv`),
4 rows handled specially (below). The header + all 3 rows covered by your target
sample come out **byte-for-byte identical** to `target_orders.csv`.

## Transformations applied (all confirmed by your target sample)

| Field | Rule |
|---|---|
| header names | trimmed & renamed: `customer`→`customer_name`, `amt`→`amount`, `active`→`is_active`, `zip`→`postal_code` |
| file | stripped UTF-8 BOM; mixed CRLF/LF tolerated; output is LF, no BOM |
| `amount` | strip `$` and thousands commas; `(500)` accounting notation → `-500.00`; always 2 decimals |
| `order_date` | `MM/DD/YYYY` → `YYYY-MM-DD` |
| `is_active` | `Y`→`true`, `N`→`false` |
| `postal_code` | kept as text — **leading zeros preserved** (e.g. `02134`) |
| `customer_name` | double-encoded UTF-8 repaired (`JosÃ©`→`José`); Unicode normalized to NFC (`Réné`) |
| `notes` | `N/A` and whitespace-only → empty |

## Please review these choices (I made reasonable calls while you were away)

1. **Duplicate order `10000005`** — appeared twice, fully identical. I **kept the
   first and dropped the second**, since `order_id` is the warehouse key and two
   identical rows would collide. If these are genuinely two distinct orders, this
   is wrong — but identical content strongly suggests a double-export artifact.
   (Dropped row preserved in `output/rejects.csv`.)

2. **Ragged row `10000006` (Carol)** — had only 6 columns instead of 7 (no `notes`
   field). Rather than silently pad or shift columns, I **quarantined it** to
   `output/rejects.csv`. If the missing field is simply an omitted `notes`, it's
   safe to re-admit with empty notes — your call.

3. **Footer row `Total,,3222.56,...`** — treated as a summary line and excluded
   (non-numeric order_id). Note: that footer total (3222.56) does **not** match the
   sum of the real amounts (2222.56); it appears the vendor added `(500)` as +500
   instead of −500. Your target sample confirms `(500)` = −500, so the data rows are
   right and the vendor's footer is the thing that's off. No action needed unless
   you reconcile against that footer downstream.

4. **Mojibake repair is heuristic** — only triggers on clear double-encoding markers
   and only when the repair round-trips cleanly (correctly-encoded names are left
   untouched). Here it fired once (`JosÃ© GarcÃ­a` → `José García`). Spot-check.

5. **Date format** — parsed as US `MM/DD/YYYY`, which your target sample confirms.
   All source dates fit that; just be aware a future DD/MM batch would misread.

## Files
- `output/orders_warehouse.csv` — the deliverable (5 rows, target schema)
- `output/rejects.csv` — the 3 non-loaded rows, each with a reason + raw text
- `output/run_report.json` — machine-readable counts, per-fix tallies, review flags
- `etl_orders.py` — the transform (stdlib-only, deterministic, re-runnable)
