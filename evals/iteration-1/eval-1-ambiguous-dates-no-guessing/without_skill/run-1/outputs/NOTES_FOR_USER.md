# Shipment feed ETL — run notes and open questions

## What I built

`etl.py` — a self-contained Python 3 pipeline (stdlib only) that maps the partner feed
(`ship_id,date,wt,carrier,status`) into your `shipments` schema, quarantines rows that
would violate constraints, and emits a full audit trail.

Run it with:

```
python3 etl.py partner_feed.csv --out-dir OUT [--date-format {MDY,DMY,auto}] \
       [--weight-sentinels 9999] [--db shipments.db]
```

Artifacts from the sample run (in this folder):

- `shipments_loaded.csv` — load-ready rows matching the target columns (empty cell = NULL)
- `shipments.db` — SQLite database with the target DDL, actually loaded (demo target)
- `quarantine.csv` — rejected source rows with reasons
- `run_report.json` — every transformation, warning, and reject, machine-readable

Sample run result: 8 data rows read, 7 loaded, 1 quarantined, exit 0.

## IMPORTANT — one decision I could not make for you

**The date format of this feed is genuinely ambiguous, so I did not guess.**

Every date in the sample (e.g. `03/04/2026`, `05/06/2026`) has both components <= 12,
so it is valid as both MM/DD/YYYY and DD/MM/YYYY, and nothing in the file settles which
one the partner uses. Silently picking one would corrupt `ship_date` for every row with
no error anywhere — the worst kind of bug.

What the pipeline does instead, in `auto` mode (the default):

1. It scans the whole file first. If any row proves the format (a component > 12),
   it uses that format for the entire feed.
2. If the format cannot be proven — as with this sample — ambiguous dates load as
   **NULL** (the column is nullable), and each one is logged in `run_report.json` as
   `ambiguous_date_not_guessed`. The raw date strings are preserved there, so nothing
   is lost.

**Action for you:** ask the partner whether dates are MM/DD/YYYY or DD/MM/YYYY, then
re-run with `--date-format MDY` or `--date-format DMY`. Both modes are tested and work
on the sample. In future months a single date like `13/01/2026` will let `auto` mode
prove the format on its own.

## Other issues found in the sample, and how they were handled

| Issue | Rows | Handling |
|---|---|---|
| Missing `ship_id` (target is NOT NULL) | source row 7 | Quarantined to `quarantine.csv`, not loaded |
| `wt = 9999` looks like a missing-weight sentinel (appears only on undelivered rows, always exactly 9999) | rows 3, 5, 8 | Loaded as NULL, flagged `sentinel_weight_nulled`. If 9999 is a real weight, re-run with `--weight-sentinels ''` |
| Carrier casing inconsistent (`ups`, `Ups`, `FEDEX`) | 4 rows | Normalized to canonical `UPS` / `FedEx` / `DHL`; unknown carriers pass through as-is (flagged) |
| `status` Y/N | all rows | Mapped to boolean true/false (also accepts yes/no/true/false/1/0) |
| Stray whitespace (` 8.25 `) | row 4 | Trimmed |
| Trailing blank line | 1 | Skipped |

## Assumptions I made (please confirm)

1. `wt` is already in kilograms (column mapped directly to `weight_kg`; no unit
   conversion applied).
2. `9999` is a missing-data sentinel, not a real 9,999 kg shipment (reversible via flag).
3. Carrier names should be canonicalized rather than loaded verbatim.
4. A row missing `shipment_id` should be quarantined for follow-up with the partner
   rather than dropped or given a synthetic ID.

## Guardrails also built in (not triggered by this sample)

- `shipment_id` longer than 12 chars -> quarantined (VARCHAR(12))
- Duplicate `shipment_id` -> loaded but flagged (table has no PK)
- Weight > 999999.99 or negative -> NULL + flagged (DECIMAL(8,2) bounds)
- Weights with >2 decimals -> rounded to 2 dp + flagged
- Carrier > 20 chars -> truncated + flagged (VARCHAR(20))
- Unparseable dates / 2-digit years -> NULL + flagged (no guessing on century either)
- Missing expected columns in the header -> fatal error, nothing loaded
