# Review notes — orders_export.csv → warehouse format

Ran `transform_orders.py` against `eval-inputs/orders_export.csv`.
Output: `orders_transformed.csv` (6 rows). Header + first 3 rows are
byte-identical to `eval-inputs/target_orders.csv`. Full accounting is in
`run_summary.json`; every rejected line is in `quarantine.csv`.

Everything below is a choice I made because you were away. The ones marked
**CONFIRM** change meaning and are worth a look.

## Choices that change meaning — please CONFIRM

1. **Date format = MM/DD/YYYY.** Every source date has both day and month
   ≤ 12 (01/02, 03/04, 05/06, …), so the format is genuinely ambiguous on its
   own. I resolved it as month-first purely because the target sample requires
   it (`01/02/2026` → `2026-01-02` = Jan 2). If any upstream date actually has
   day > 12 in a future export, that assumption needs re-checking.

2. **Duplicate order dropped.** `10000005` (BobSmith) appears twice,
   byte-for-byte identical. I kept the first and dropped the second, treating
   `order_id` as a unique key. If duplicate lines are ever legitimate in this
   feed (e.g. partial shipments), switch this off.

3. **"N/A" note treated as empty.** Row `10000003`'s note `N/A` was emptied
   (the target sample shows it blank), along with whitespace-only notes
   (row `10000004`). Sentinels I clear: N/A, NA, NULL, NONE, NIL, "-", "--",
   blank. Confirm none of these are meaningful values in your notes column.

## Choices that are safe but worth knowing

4. **Encoding repairs on `customer_name`:**
   - `José García` arrived double-encoded as `JosÃ© GarcÃ­a` (mojibake) — repaired.
   - `Réné` arrived with combining accents (NFD); normalised to precomposed
     (NFC) to match the target.
   - `Ann Lee` had a non-breaking space; converted to a normal space.
   - `BobSmith` had an embedded vertical-tab control char between "Bob" and
     "Smith"; the control char was stripped, giving `BobSmith` (no space
     inserted — there was no evidence one belonged there).

5. **Ragged row padded.** Row `10000006` had only 6 fields (trailing `notes`
   missing). I padded `notes` to empty rather than dropping the order. Only a
   *trailing* missing field is padded; any other shape mismatch is quarantined,
   not guessed.

6. **Amounts** normalised to 2 decimals: `$` and thousands commas stripped;
   parentheses read as negative (`(500)` → `-500.00`, accounting notation).

7. **Excluded non-data lines:** the trailing `Total,,3222.56,,,,` footer and a
   blank line were dropped as non-data (order_id not an 8-digit id). See
   `quarantine.csv`. Note the footer's stated total (3222.56) does **not** match
   the sum of the kept order amounts — expected, since it was computed over the
   raw file including the duplicate; not used for anything.

8. **Structural:** BOM stripped, mixed CRLF/LF line endings handled, output is
   BOM-free UTF-8 with LF endings, leading zeros in `postal_code` preserved
   (kept as text, e.g. `02134`).

## Ignored files
`eval-inputs/partner_feed.csv`, `vendor_orders_sample.csv`, and
`vendor_orders.etlspec.yaml` are different schemas (shipments / a different
orders layout) and were not part of this transform.
