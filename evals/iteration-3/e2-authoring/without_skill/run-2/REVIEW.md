# Orders ETL — run notes & things to review

**Script:** `transform_orders.py` (stdlib only, Python 3)
**Input:** `eval-inputs/orders_export.csv` (vendor export)
**Target format sample:** `eval-inputs/target_orders.csv`
**Outputs:** `output/orders_warehouse.csv`, `output/orders_rejects.csv`

**Run outcome:** 9 data lines in → **6 warehouse rows out**, 0 rejects, exit 0.
The first 3 output rows match your `target_orders.csv` sample **byte-for-byte**.

## Column mapping (source → warehouse)
| source header (raw)     | warehouse field |
|-------------------------|-----------------|
| `order_id`              | `order_id`      |
| ` customer ` (spaces)   | `customer_name` |
| `amt`                   | `amount`        |
| `order_date`            | `order_date`    |
| `active`                | `is_active`     |
| `zip`                   | `postal_code`   |
| `notes`                 | `notes`         |

## Transformations applied (each inferred from the target sample)
- **BOM + mixed line endings**: source has a UTF-8 BOM and mixes CRLF/LF; read with `utf-8-sig`, output written with clean `\n` to match the target.
- **amount**: strip `$` and thousands `,`; accounting parentheses `(500)` → `-500.00`; format to 2 decimals.
- **order_date**: `MM/DD/YYYY` → ISO `YYYY-MM-DD`.
- **is_active**: `Y`/`N` → `true`/`false`.
- **postal_code**: kept as a string to preserve leading zeros (e.g. `02134`).
- **notes**: trimmed; null-ish tokens (`N/A`, blank, whitespace-only) → empty string.
- **customer_name — encoding repair**: `JosÃ© GarcÃ­a` (UTF-8 mis-decoded as Latin-1) → `José García`. Repair only fires when mojibake markers are present and the round-trip decodes cleanly, so already-correct names are untouched.
- **customer_name — Unicode NFC**: the source mixes normalization forms (`José` was precomposed, `Réné` was decomposed `e`+combining-accent). All text normalized to **NFC** to match the target.

## Choices I made while you were away — PLEASE REVIEW
1. **Date format assumed MM/DD/YYYY.** The digits are ambiguous (all day/month values ≤ 12), but your `target` sample proves it: `01/02/2026` → `2026-01-02`. Low risk, but confirm the vendor isn't sending DD/MM.
2. **Exact-duplicate row dropped.** `order_id 10000005` (BobSmith) appears twice, fully identical. Treated as a double-export artifact and de-duplicated (kept first), since `order_id` is the warehouse key. If duplicates are legitimate, remove the de-dupe step. *No data is lost silently — it's in the run log and the raw export.*
3. **Footer/`Total` row excluded.** The `Total,,3222.56,...` line is a vendor summary, not an order. Note: its `3222.56` is **wrong for our purposes** — it counts the duplicate row and treats `(500)` as `+500`. Do **not** reconcile against it. (Correct sum of the 6 loaded rows = `2147.56`.)
4. **Ragged row padded.** `order_id 10000006` (Carol) had only 6 of 7 columns (missing trailing `notes`); padded `notes` to empty. If a short row should instead be quarantined, change the ragged-row handling.
5. **Blank line skipped** (row 8 was empty).

## Safety behaviors built in
- Anything unparseable goes to `output/orders_rejects.csv` with a reason (currently empty — nothing was rejected).
- Every auto-fix, skip, drop, and pad is counted and printed on each run.
- Re-running is deterministic and idempotent (same input → same output).
