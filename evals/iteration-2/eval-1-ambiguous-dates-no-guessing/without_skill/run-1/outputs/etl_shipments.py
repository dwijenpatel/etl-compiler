#!/usr/bin/env python3
"""
ETL: partner monthly shipment feed -> `shipments` target table.

Target schema (authoritative):
    CREATE TABLE shipments (
      shipment_id VARCHAR(12) NOT NULL,
      ship_date   DATE,
      weight_kg   DECIMAL(8,2),
      carrier     VARCHAR(20),
      delivered   BOOLEAN
    );

Source columns -> target columns:
    ship_id -> shipment_id
    date    -> ship_date
    wt      -> weight_kg
    carrier -> carrier
    status  -> delivered   (Y=true, N=false)

Design principles for this load (the partner is not reachable to answer
questions, so we optimize for *not silently corrupting data*):

  * Every input row is accounted for: loaded, quarantined, or skipped-blank.
    Counts reconcile at the end.
  * We never pad a NOT NULL violation. A row with no shipment_id is
    quarantined with its raw contents preserved, so it can be reprocessed
    once the partner supplies the id.
  * Transformations that cannot change meaning (whitespace trim, Y/N ->
    boolean) are applied and counted.
  * Transformations that *could* change meaning are NOT guessed. Instead the
    raw value is loaded as-is and the row/column is added to REVIEW_REQUIRED:
      - Ambiguous date format (see DATE_FORMAT_ASSUMPTION below).
      - Suspected sentinel weights (9999) that look like "unknown", not a
        real 9999 kg measurement.
      - Inconsistent carrier casing (UPS/ups/Ups, FedEx/FEDEX) is reported
        but the raw text is preserved (canonicalization is a partner call).

Stdlib only. Loads into an in-process SQLite db that mirrors the DDL so the
NOT NULL constraint is actually enforced, then exports the loaded rows.
"""

import csv
import json
import os
import sqlite3
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "eval-inputs", "partner_feed.csv")

OUT_DB = os.path.join(HERE, "shipments.db")
OUT_LOADED = os.path.join(HERE, "shipments_loaded.csv")
OUT_QUARANTINE = os.path.join(HERE, "shipments_quarantine.csv")
OUT_REPORT = os.path.join(HERE, "shipments_load_report.json")

# ---------------------------------------------------------------------------
# Assumptions that a human should confirm. Each is surfaced in the report.
# ---------------------------------------------------------------------------

# The feed's dates all have day AND month <= 12, so MM/DD/YYYY vs DD/MM/YYYY
# is genuinely undecidable from the data. We assume US month-first because the
# other fields read as a US partner (Y/N flags, carriers UPS/FedEx/DHL), but
# this is an ASSUMPTION, not a fact: change this one constant to re-run under
# the other interpretation. The raw date string is preserved in the report and
# every parsed date is listed under review_required.
DATE_FORMAT_ASSUMPTION = "%m/%d/%Y"  # month-first; alt: "%d/%m/%Y"

# Weight values that are almost certainly a "missing/unknown" sentinel rather
# than a genuine measurement. Loaded as-is (non-destructive) but flagged.
SUSPECTED_WEIGHT_SENTINELS = {Decimal("9999")}

# Target-schema limits we enforce ourselves (SQLite doesn't enforce VARCHAR
# length or DECIMAL precision).
SHIPMENT_ID_MAXLEN = 12
CARRIER_MAXLEN = 20
WEIGHT_MAX = Decimal("999999.99")  # DECIMAL(8,2) -> 6 integer + 2 fractional
WEIGHT_SCALE = Decimal("0.01")

DDL = """
CREATE TABLE shipments (
  shipment_id TEXT NOT NULL,   -- VARCHAR(12) NOT NULL (length enforced in Python)
  ship_date   TEXT,            -- DATE, stored ISO-8601 yyyy-mm-dd
  weight_kg   TEXT,            -- DECIMAL(8,2), stored as canonical string
  carrier     TEXT,            -- VARCHAR(20)
  delivered   INTEGER          -- BOOLEAN 0/1
);
"""


class RowError(Exception):
    """Raised when a row cannot be loaded and must be quarantined."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def parse_shipment_id(raw):
    val = (raw or "").strip()
    if val == "":
        raise RowError("MISSING_REQUIRED", "shipment_id is empty but column is NOT NULL")
    if len(val) > SHIPMENT_ID_MAXLEN:
        raise RowError(
            "TOO_LONG",
            f"shipment_id {val!r} exceeds VARCHAR({SHIPMENT_ID_MAXLEN})",
        )
    return val, (val != (raw or ""))  # (value, was_trimmed)


def parse_date(raw, review, rownum):
    val = (raw or "").strip()
    if val == "":
        return None, False  # ship_date is nullable; genuine blank -> NULL
    try:
        dt = datetime.strptime(val, DATE_FORMAT_ASSUMPTION)
    except ValueError:
        raise RowError("BAD_DATE", f"date {val!r} not parseable as {DATE_FORMAT_ASSUMPTION}")
    iso = dt.strftime("%Y-%m-%d")
    # Ambiguous format: every value here is undecidable, so always flag.
    review.append(
        {
            "row": rownum,
            "column": "ship_date",
            "issue": "ambiguous_date_format",
            "raw": val,
            "loaded_as": iso,
            "assumption": DATE_FORMAT_ASSUMPTION,
            "detail": "day and month both <= 12; MM/DD vs DD/MM cannot be "
            "determined from data. Confirm with partner.",
        }
    )
    return iso, (val != (raw or ""))


def parse_weight(raw, review, rownum):
    stripped = (raw or "").strip()
    was_trimmed = stripped != (raw or "")
    if stripped == "":
        return None, was_trimmed  # nullable
    try:
        dec = Decimal(stripped)
    except InvalidOperation:
        raise RowError("BAD_DECIMAL", f"weight {stripped!r} is not a number")
    if dec < 0:
        raise RowError("BAD_DECIMAL", f"weight {stripped!r} is negative")
    if dec > WEIGHT_MAX:
        raise RowError("OUT_OF_RANGE", f"weight {stripped} exceeds DECIMAL(8,2) max {WEIGHT_MAX}")
    # Quantize to scale 2 (schema is DECIMAL(8,2)); flag if it actually changed.
    quantized = dec.quantize(WEIGHT_SCALE, rounding=ROUND_HALF_UP)
    if quantized != dec:
        review.append(
            {
                "row": rownum,
                "column": "weight_kg",
                "issue": "precision_rounded",
                "raw": stripped,
                "loaded_as": str(quantized),
                "detail": "value had >2 decimal places; rounded to DECIMAL(8,2).",
            }
        )
    if dec in SUSPECTED_WEIGHT_SENTINELS:
        review.append(
            {
                "row": rownum,
                "column": "weight_kg",
                "issue": "suspected_sentinel",
                "raw": stripped,
                "loaded_as": str(quantized),
                "detail": "9999 recurs and is far outside the range of real "
                "weights (3.75-140 kg); likely a 'missing weight' "
                "sentinel. Loaded as-is; confirm with partner whether "
                "it should be NULL.",
            }
        )
    return str(quantized), was_trimmed


def parse_carrier(raw, casing_index):
    val = (raw or "").strip()
    was_trimmed = val != (raw or "")
    if val == "":
        return None, was_trimmed
    if len(val) > CARRIER_MAXLEN:
        raise RowError("TOO_LONG", f"carrier {val!r} exceeds VARCHAR({CARRIER_MAXLEN})")
    casing_index.setdefault(val.upper(), set()).add(val)
    return val, was_trimmed


def parse_delivered(raw):
    val = (raw or "").strip()
    up = val.upper()
    truthy = {"Y", "YES", "TRUE", "T", "1"}
    falsy = {"N", "NO", "FALSE", "F", "0"}
    if up in truthy:
        return 1, (val != (raw or ""))
    if up in falsy:
        return 0, (val != (raw or ""))
    if val == "":
        return None, False  # nullable
    raise RowError("BAD_BOOLEAN", f"status {val!r} is not a recognized boolean")


def main():
    review = []           # meaning-affecting items needing human confirmation
    quarantine = []       # rows that could not be loaded
    loaded = []           # (shipment_id, ship_date, weight_kg, carrier, delivered)
    carrier_casing = {}   # UPPER -> {raw variants seen}
    counters = {
        "rows_read": 0,
        "blank_rows_skipped": 0,
        "rows_loaded": 0,
        "rows_quarantined": 0,
        "whitespace_trims": 0,
    }

    with open(SRC, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        expected = ["ship_id", "date", "wt", "carrier", "status"]
        if [h.strip().lower() for h in header] != expected:
            print(f"FATAL: unexpected header {header!r}; expected {expected}", file=sys.stderr)
            return 3

        for lineno, row in enumerate(reader, start=2):  # line 1 was the header
            # Fully-blank line (e.g. trailing newline) -> skip, but account for it.
            if not row or all((c or "").strip() == "" for c in row):
                counters["blank_rows_skipped"] += 1
                continue
            counters["rows_read"] += 1

            if len(row) != len(expected):
                quarantine.append(
                    {
                        "line": lineno,
                        "code": "WRONG_FIELD_COUNT",
                        "message": f"expected {len(expected)} fields, got {len(row)}",
                        "raw": row,
                    }
                )
                continue

            raw_ship, raw_date, raw_wt, raw_carrier, raw_status = row
            try:
                ship_id, t1 = parse_shipment_id(raw_ship)
                ship_date, t2 = parse_date(raw_date, review, lineno)
                weight, t3 = parse_weight(raw_wt, review, lineno)
                carrier, t4 = parse_carrier(raw_carrier, carrier_casing)
                delivered, t5 = parse_delivered(raw_status)
            except RowError as err:
                quarantine.append(
                    {
                        "line": lineno,
                        "code": err.code,
                        "message": err.message,
                        "raw": row,
                    }
                )
                continue

            counters["whitespace_trims"] += sum(bool(x) for x in (t1, t2, t3, t4, t5))
            loaded.append((ship_id, ship_date, weight, carrier, delivered))

    # --- Load into a SQLite table mirroring the DDL (enforces NOT NULL) ------
    if os.path.exists(OUT_DB):
        os.remove(OUT_DB)
    conn = sqlite3.connect(OUT_DB)
    try:
        conn.execute(DDL)
        conn.executemany(
            "INSERT INTO shipments "
            "(shipment_id, ship_date, weight_kg, carrier, delivered) "
            "VALUES (?, ?, ?, ?, ?)",
            loaded,
        )
        conn.commit()
        db_count = conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0]
    finally:
        conn.close()

    counters["rows_loaded"] = len(loaded)
    counters["rows_quarantined"] = len(quarantine)

    # --- Carrier casing report ---------------------------------------------
    carrier_variants = {
        canon: sorted(variants)
        for canon, variants in carrier_casing.items()
        if len(variants) > 1
    }
    for canon, variants in carrier_variants.items():
        review.append(
            {
                "row": None,
                "column": "carrier",
                "issue": "inconsistent_casing",
                "raw": variants,
                "loaded_as": "raw text preserved (not canonicalized)",
                "detail": f"carrier {canon} appears with mixed casing {variants}; "
                "canonical form is a partner decision.",
            }
        )

    # --- Write loaded rows (atomic: temp then rename) -----------------------
    tmp = OUT_LOADED + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["shipment_id", "ship_date", "weight_kg", "carrier", "delivered"])
        for r in loaded:
            w.writerow(["" if c is None else c for c in r])
    os.replace(tmp, OUT_LOADED)

    # --- Write quarantine ---------------------------------------------------
    tmp = OUT_QUARANTINE + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["src_line", "error_code", "error_message", "raw_row"])
        for q in quarantine:
            w.writerow([q["line"], q["code"], q["message"], "|".join(q["raw"])])
    os.replace(tmp, OUT_QUARANTINE)

    # --- Write JSON run report / manifest -----------------------------------
    reconciled = (
        counters["rows_read"]
        == counters["rows_loaded"] + counters["rows_quarantined"]
    )
    report = {
        "source": os.path.relpath(SRC, HERE),
        "target_table": "shipments",
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "counts": counters,
        "counts_reconcile": reconciled,
        "db_row_count": db_count,
        "quarantined_rows": quarantine,
        "review_required": review,
        "assumptions": {
            "date_format": DATE_FORMAT_ASSUMPTION,
            "date_format_note": "All feed dates have day and month <= 12; the "
            "format is undecidable from the data. Every ship_date is listed in "
            "review_required. Re-run with DATE_FORMAT_ASSUMPTION='%d/%m/%Y' to "
            "test the day-first interpretation.",
            "weight_sentinels": sorted(str(s) for s in SUSPECTED_WEIGHT_SENTINELS),
            "weight_sentinel_note": "9999 loaded as-is, NOT converted to NULL, "
            "to avoid destroying data; flagged for partner confirmation.",
            "carrier_casing": "raw casing preserved; not canonicalized.",
            "boolean_mapping": {"true": "Y/YES/TRUE/T/1", "false": "N/NO/FALSE/F/0"},
        },
    }
    tmp = OUT_REPORT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    os.replace(tmp, OUT_REPORT)

    # --- Console summary ----------------------------------------------------
    print("=" * 66)
    print("ETL run: partner_feed.csv -> shipments")
    print("=" * 66)
    print(f"  rows read (non-blank) : {counters['rows_read']}")
    print(f"  rows loaded           : {counters['rows_loaded']}  (db rows: {db_count})")
    print(f"  rows quarantined      : {counters['rows_quarantined']}")
    print(f"  blank rows skipped    : {counters['blank_rows_skipped']}")
    print(f"  whitespace trims      : {counters['whitespace_trims']}")
    print(f"  counts reconcile      : {reconciled}")
    print(f"  review-required items : {len(review)}")
    print("-" * 66)
    if quarantine:
        print("QUARANTINED (not loaded):")
        for q in quarantine:
            print(f"  line {q['line']}: [{q['code']}] {q['message']}")
    if review:
        print("REVIEW REQUIRED before trusting the load:")
        seen = set()
        for r in review:
            key = (r["column"], r["issue"])
            if key in seen:
                continue
            seen.add(key)
            print(f"  - {r['column']}: {r['issue']}")
    print("-" * 66)
    print(f"  loaded rows -> {os.path.relpath(OUT_LOADED, HERE)}")
    print(f"  quarantine  -> {os.path.relpath(OUT_QUARANTINE, HERE)}")
    print(f"  report      -> {os.path.relpath(OUT_REPORT, HERE)}")
    print(f"  sqlite db   -> {os.path.relpath(OUT_DB, HERE)}")
    print("=" * 66)

    if not reconciled:
        return 3  # accounting bug -> hard fail
    # Exit 2 when the run completed but produced quarantine/review items,
    # so an orchestrator can distinguish "clean" from "completed-with-issues".
    return 2 if (quarantine or review) else 0


if __name__ == "__main__":
    sys.exit(main())
