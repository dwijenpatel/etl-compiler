# partner_shipments ETL — run notes (unattended mode)

Generated 2026-07-17 by the etl-generator skill (taxonomy v0.1) from the sample
`/home/claude/work/eval-inputs/partner_feed.csv` against the `shipments` DDL you provided.
You were unavailable for the interview, so this ran in **unattended mode**: every
meaning-changing (ask-class) finding got the *safest* option — the one that
**quarantines rather than reinterprets data** — and is marked
`provenance: unconfirmed` in the spec for your review.

## ACTION REQUIRED before this pipeline is useful

**TYP-03 — date format is fully ambiguous, and I did not guess.**
All 8 sample values of `date` (e.g. `03/04/2026`) parse as *both* MDY (Mar 4) and
DMY (Apr 3); no value has a day > 12 to disambiguate. A wrong guess would silently
corrupt every date, so the format is left **undeclared**: every row with a date
currently quarantines with error code `TYP-03` (raw rows preserved in
`quarantine.csv` — nothing is lost).

To fix (one decision, two edits):
1. Confirm with the partner: US `MDY` (`%m/%d/%Y`) or international `DMY` (`%d/%m/%Y`).
2. In `partner_shipments.etlspec.yaml`, set that format in the `ship_date` mapping's
   `to_date.formats`, change its provenance to `explicit`, and regenerate — or
   equivalently set `DATE_FORMATS` in `partner_shipments_pipeline.py` to match.
3. Re-run: `python partner_shipments_pipeline.py partner_feed.csv --out-dir ./etl_out`

A scratch smoke test with a hypothetical format confirmed the rest of the pipeline
then works end-to-end (4 rows load cleanly; see "After the date fix" below).

## Decisions flagged for review (all `provenance: unconfirmed`)

| ID | Column | What I did | What you should decide |
|---|---|---|---|
| TYP-03 | `date` | No format declared; all dated rows quarantine | MDY or DMY? |
| NUL-03 | `wt` | `9999` (3 of 8 rows, always with `status=N`) treated as *suspected* null-sentinel: rows **quarantine** — not nulled, not loaded as a real 9999.00 kg weight | Is `9999` "unknown weight" (→ null) or real data (→ load)? |
| NUL-01 | `ship_id` | Empty value → null → row quarantines as `NUL-04` (target is `NOT NULL`) | Should ID-less rows be repaired upstream, or dropped? |
| TYP-06 | `status` | `Y`→true, `N`→false (natural reading); anything else is a row-error | Confirm the truth mapping |
| TYP-10 | `carrier` | Case variants (`UPS`/`Ups`/`ups`, `FedEx`/`FEDEX`) **preserved verbatim** — canonicalization changes content | Pick a canonical casing if you want one (e.g. uppercase) |

## Defaulted policies (taxonomy house defaults, recorded in the spec)

UTF-8 encoding (profiler-detected), comma delimiter, NFC normalization, control-char
strip, unicode-whitespace normalization, whitespace trim (this is what turns
`" 8.25 "` into `8.25`), SQL null propagation, ISO-8601 date rendering, quarantine
error disposition, 5%/min-100-rows error budget, duplicates kept.
Schema-derived guards: `TYP-11` length checks (VARCHAR 12/20), `TYP-08` strict
scale-2 decimals (no silent rounding), `TYP-09` DECIMAL(8,2) range bound.

## Run results on the sample (this delivery)

Command: `python partner_shipments_pipeline.py partner_feed.csv --out-dir ./etl_out`

- **Exit code 2** (runtime convention: completed, with quarantined rows)
- rows in: 8 - rows out: **0** - quarantined: **8**
  - 7 × `TYP-03` (`date` — format undeclared, see ACTION REQUIRED)
  - 1 × `NUL-04` (`ship_id` empty, target `NOT NULL`)
- Artifacts in `etl_out/`: `output.csv` (header only), `quarantine.csv` (raw rows,
  reprocessable), `errors.jsonl` (per-row errors), `summary.json`, `manifest.json`
  (input SHA-256, effective policies, counts).

## After the date fix (verified by scratch smoke test)

With a declared date format and no other decision changed, the sample yields:
- 4 rows loaded (SH-0001, SH-0003, SH-0005, SH-0008)
- 3 rows quarantined `NUL-03` (`wt = 9999`) — resolves once you rule on the sentinel
- 1 row quarantined `NUL-04` (empty `ship_id`)

## Files delivered

- `partner_shipments.etlspec.yaml` — the spec; **source of truth**. Edit it and
  regenerate rather than hand-editing transform logic.
- `partner_shipments_pipeline.py` — generated pipeline (thin orchestration).
- `etl_runtime.py` — shared runtime, copied verbatim from the skill; do not fork.
- `findings.json` — profiler findings that drove the decisions above.
- `etl_out/` — run artifacts from executing the pipeline on the sample.

One implementation note: the unconfirmed `9999` sentinel rejection is a small,
loudly-commented guard inside `transform_row` (raising the runtime's `RowError`
with code `NUL-03`) rather than a runtime `sentinels` policy entry, because the
runtime's sentinel mechanism *nulls* matches — and nulling is exactly the
reinterpretation that must not happen before you confirm. Once you confirm `9999`
is a null-sentinel, delete the guard and add `"sentinels": {"wt": ["9999"]}` to
the pipeline policies (and the spec's `ship_date`-style per-column `sentinels`
entry with `provenance: explicit`).
