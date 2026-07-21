# Orders export -> warehouse format: run notes & review items

Transformed `eval-inputs/orders_export.csv` into the warehouse format shown by
`eval-inputs/target_orders.csv`.

- **Script:** `transform_orders.py` (Python 3, standard library only)
- **Output:** `orders_transformed.csv` (7 rows) — first 3 rows are **byte-identical** to the provided target sample
- **Rejected/excluded rows:** `orders_rejected.csv`
- **Machine-readable run log:** `run_summary.json`
- **Run:** `python3 transform_orders.py` — exit code **2** = "completed, but items below need a look" (0 would mean fully clean)

## Column mapping (source -> target)

| source header      | target        | transform |
|--------------------|---------------|-----------|
| `order_id`         | `order_id`    | trimmed; kept as string |
| ` customer ` *(note stray spaces)* | `customer_name` | trim, mojibake repair, control-char scrub, NFC |
| `amt`              | `amount`      | strip `$`/`,`; `(n)` = negative; 2 decimal places |
| `order_date`       | `order_date`  | `MM/DD/YYYY` -> `YYYY-MM-DD` |
| `active`           | `is_active`   | `Y`->`true`, `N`->`false` |
| `zip`              | `postal_code` | trim; kept as string (leading zeros preserved) |
| `notes`            | `notes`       | trim; sentinels -> empty |

## Choices I made (defaults you may want to confirm)

1. **Date format is MM/DD/YYYY (US, month-first).** The target sample forces
   this: `01/02/2026` -> `2026-01-02`. If the source system is actually
   day-first, every date is wrong — this is the single most important thing to
   confirm. (All source dates here have day <= 12, so the raw data alone can't
   disambiguate.)

2. **Amounts normalized to 2 decimal places.** `$1,234.56`->`1234.56`,
   `(500)`->`-500.00` (accounting parentheses = negative), `1000`->`1000.00`.

3. **Mojibake repaired in `customer_name`.** Row for order `10000003` was
   double-encoded (`JosÃ© GarcÃ­a`) and is restored to `José García`, matching
   the target. Correctly-encoded names (`José`, `Réné`) are left untouched — the
   repair only fires when tell-tale bytes are present and the fix cleanly
   round-trips.

4. **Null sentinels in `notes` -> empty.** `N/A` and whitespace-only notes become
   empty, matching the target (`N/A` -> `` for order `10000003`).

5. **`Total,,3222.56,,,,` footer row dropped.** Its `order_id` is non-numeric,
   so it's a summary line, not an order. Excluded from the data output and
   written to `orders_rejected.csv` (reason `non_data_row`).

   **Reconciliation note (important):** the footer total `3222.56` matches the
   detail rows *only* if `(500)` is counted as **+500** and the duplicate `75`
   **is** included (`1234.56 + 500 + 250 + 1000 + 75 + 88 + 75 = 3222.56`).
   Two implications:
   - The vendor's own total treats parentheses as **positive**, but the target
     warehouse format treats `(500)` as **negative** (`-500.00`). So a
     warehouse-vs-vendor total reconciliation will legitimately differ by
     ~$1000 on that order. Confirm the warehouse's sign convention is intended.
   - The vendor total counts the duplicate `10000005`, which is (weak) evidence
     the duplicate may be intentional rather than an export artifact — another
     reason I kept it rather than dropping it.

## Rows flagged for your review

- **Order `10000005` appears twice, byte-for-byte identical (source lines 6 & 8).**
  I **kept both** rather than silently drop data. If `order_id` is a primary key
  in the warehouse (very likely), delete one — it will otherwise double-count.
  This was left as a keep-and-flag decision because de-duplicating changes
  meaning and you're away.

- **Hidden control character in a customer name.** The `10000005` rows contain a
  vertical tab (U+000B) between "Bob" and "Smith", stored as `Bob<VT>Smith`. I
  stripped it, yielding `BobSmith`. **It may have been meant as a space
  (`Bob Smith`)** — I did not invent a space; please confirm the intended
  spelling.

- **Short row padded.** Order `10000006` (source line 7) had only 6 of 7 fields
  (missing the trailing `notes`); I padded `notes` empty. The other fields look
  fine.

## Things worth noting about the source file

- Encoded as **UTF-8 with a BOM** and has **mixed CRLF/LF** line endings; both
  handled. Output is plain UTF-8 with LF (matching the target sample).
- One blank line was skipped.

## Reproduce

```
python3 transform_orders.py \
  --input eval-inputs/orders_export.csv \
  --output orders_transformed.csv
```
