#!/usr/bin/env python3
"""
ETL: partner monthly shipment feed  ->  `shipments` target table.

Target DDL
----------
CREATE TABLE shipments (
  shipment_id VARCHAR(12) NOT NULL,
  ship_date   DATE,
  weight_kg   DECIMAL(8,2),
  carrier     VARCHAR(20),
  delivered   BOOLEAN
);

Source columns (partner_feed.csv):  ship_id, date, wt, carrier, status

No live database connection was provided, so "load" here means: transform +
validate every source row and materialize the result as it *would* be inserted.
Valid rows are written to shipments_loaded.csv (typed to the target columns),
rejected rows to shipments_rejected.csv (with a reason), and a machine-readable
run summary to run_summary.json. Exit code reflects the outcome (see bottom).

Careful-mode decisions (the operator was unavailable to confirm)
---------------------------------------------------------------
1. DATE FORMAT IS AMBIGUOUS. Every source date has day and month <= 12
   (e.g. 03/04/2026), so MM/DD/YYYY vs DD/MM/YYYY cannot be decided from the
   data. This is a meaning-changing choice. Assumption: MM/DD/YYYY (US format),
   chosen because the carriers (UPS, FedEx, DHL) and "partner feed" framing are
   US-centric. This assumption is UNCONFIRMED and is flagged in the run summary
   under `review_required`. If the partner is non-US, re-run with
   DATE_FORMAT = "%d/%m/%Y".
2. weight_kg == 9999 is treated as a MISSING-VALUE SENTINEL, not a real weight.
   It recurs exactly and is implausible (~10 tonnes) next to 3.75-140 kg
   shipments. Such rows are LOADED with weight_kg = NULL (the column is
   nullable) rather than reject or store a false 9999.00. Every substitution is
   counted and listed under `review_required`.
3. Carrier names are canonicalized case-insensitively to known spellings
   (UPS, FedEx, DHL). Unknown carriers pass through trimmed, with a warning.
   Nothing is dropped on account of carrier.
4. A row with an empty shipment_id VIOLATES the NOT NULL constraint and is
   REJECTED (no id is invented).
"""

import csv
import json
import os
import sys
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

# ---------------------------------------------------------------- config ----
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "eval-inputs", "partner_feed.csv")
OUT_LOADED = os.path.join(HERE, "shipments_loaded.csv")
OUT_REJECTED = os.path.join(HERE, "shipments_rejected.csv")
OUT_SUMMARY = os.path.join(HERE, "run_summary.json")

DATE_FORMAT = "%m/%d/%Y"          # ASSUMPTION -- see module docstring, note 1
DATE_FORMAT_LABEL = "MM/DD/YYYY (assumed, unconfirmed)"
WEIGHT_SENTINELS = {Decimal("9999")}   # note 2

# Target column constraints
ID_MAXLEN = 12
CARRIER_MAXLEN = 20
DECIMAL_QUANT = Decimal("0.01")
DECIMAL_MAX = Decimal("999999.99")     # DECIMAL(8,2)

# Canonical carrier spellings, keyed by uppercased/trimmed form
CARRIER_CANON = {"UPS": "UPS", "FEDEX": "FedEx", "DHL": "DHL"}

# Boolean mapping for `status` -> `delivered`
TRUE_TOKENS = {"Y", "YES", "TRUE", "T", "1", "DELIVERED"}
FALSE_TOKENS = {"N", "NO", "FALSE", "F", "0"}


# ------------------------------------------------------------- accounting ---
# Every modification and every drop is tallied so the run is fully accountable.
counters = {
    "rows_read": 0,          # data rows (excludes header / blank lines)
    "rows_loaded": 0,
    "rows_rejected": 0,
    "weight_whitespace_trimmed": 0,
    "weight_sentinel_nulled": 0,
    "carrier_canonicalized": 0,
    "carrier_unknown": 0,
}
warnings = []       # per-row, per-column notes (non-fatal)
review_required = []  # decisions the operator should confirm


def note(row_num, shipment_id, column, code, detail):
    warnings.append({
        "row": row_num, "shipment_id": shipment_id,
        "column": column, "code": code, "detail": detail,
    })


# -------------------------------------------------------------- transforms --
def clean_id(raw, row_num, errors):
    v = (raw or "").strip()
    if not v:
        errors.append("shipment_id is empty but target column is NOT NULL")
        return None
    if len(v) > ID_MAXLEN:
        errors.append(f"shipment_id '{v}' exceeds VARCHAR({ID_MAXLEN})")
        return None
    return v


def clean_date(raw, row_num, shipment_id, errors):
    v = (raw or "").strip()
    if not v:
        return None  # nullable
    try:
        d = datetime.strptime(v, DATE_FORMAT).date()
    except ValueError:
        errors.append(f"ship_date '{v}' not parseable as {DATE_FORMAT_LABEL}")
        return None
    return d.isoformat()


def clean_weight(raw, row_num, shipment_id, errors):
    v = (raw or "")
    stripped = v.strip()
    if stripped != v:
        counters["weight_whitespace_trimmed"] += 1
        note(row_num, shipment_id, "weight_kg", "STR-trim",
             f"trimmed surrounding whitespace from '{v}'")
    if not stripped:
        return None  # nullable
    try:
        d = Decimal(stripped)
    except InvalidOperation:
        errors.append(f"weight_kg '{stripped}' is not a number")
        return None
    if d in WEIGHT_SENTINELS:
        counters["weight_sentinel_nulled"] += 1
        note(row_num, shipment_id, "weight_kg", "NUL-sentinel",
             f"value {stripped} treated as missing-value sentinel -> NULL")
        return None
    if d < 0:
        errors.append(f"weight_kg '{stripped}' is negative")
        return None
    d = d.quantize(DECIMAL_QUANT, rounding=ROUND_HALF_UP)
    if d > DECIMAL_MAX:
        errors.append(f"weight_kg {d} exceeds DECIMAL(8,2) range")
        return None
    return str(d)


def clean_carrier(raw, row_num, shipment_id, errors):
    v = (raw or "").strip()
    if not v:
        return None  # nullable
    canon = CARRIER_CANON.get(v.upper())
    if canon is None:
        counters["carrier_unknown"] += 1
        note(row_num, shipment_id, "carrier", "unknown-carrier",
             f"unrecognized carrier '{v}' passed through unchanged")
        result = v
    else:
        if canon != v:
            counters["carrier_canonicalized"] += 1
            note(row_num, shipment_id, "carrier", "case-canon",
                 f"carrier '{v}' -> '{canon}'")
        result = canon
    if len(result) > CARRIER_MAXLEN:
        errors.append(f"carrier '{result}' exceeds VARCHAR({CARRIER_MAXLEN})")
        return None
    return result


def clean_delivered(raw, row_num, shipment_id, errors):
    v = (raw or "").strip()
    if not v:
        return None  # nullable
    u = v.upper()
    if u in TRUE_TOKENS:
        return True
    if u in FALSE_TOKENS:
        return False
    errors.append(f"status '{v}' is not a recognized boolean token")
    return None


# -------------------------------------------------------------------- main --
def main():
    loaded, rejected = [], []

    with open(SRC, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        expected = ["ship_id", "date", "wt", "carrier", "status"]
        if [h.strip() for h in header] != expected:
            print(f"FATAL: unexpected header {header!r}, expected {expected!r}",
                  file=sys.stderr)
            return 3

        for line_no, row in enumerate(reader, start=2):
            if not any((c or "").strip() for c in row):
                continue  # skip fully-blank lines
            counters["rows_read"] += 1

            # normalize field count
            if len(row) != len(expected):
                rejected.append({
                    "row": line_no, "raw": row,
                    "reason": f"expected {len(expected)} fields, got {len(row)}",
                })
                counters["rows_rejected"] += 1
                continue

            ship_id_raw, date_raw, wt_raw, carrier_raw, status_raw = row
            errors = []

            shipment_id = clean_id(ship_id_raw, line_no, errors)
            # id is required; keep an id for logging even if others are cleaned
            log_id = shipment_id or "(missing)"
            ship_date = clean_date(date_raw, line_no, log_id, errors)
            weight_kg = clean_weight(wt_raw, line_no, log_id, errors)
            carrier = clean_carrier(carrier_raw, line_no, log_id, errors)
            delivered = clean_delivered(status_raw, line_no, log_id, errors)

            if errors:
                rejected.append({
                    "row": line_no, "raw": row,
                    "reason": "; ".join(errors),
                })
                counters["rows_rejected"] += 1
                continue

            loaded.append({
                "shipment_id": shipment_id,
                "ship_date": ship_date,
                "weight_kg": weight_kg,
                "carrier": carrier,
                "delivered": delivered,
            })
            counters["rows_loaded"] += 1

    # ---- materialize outputs ------------------------------------------------
    cols = ["shipment_id", "ship_date", "weight_kg", "carrier", "delivered"]
    with open(OUT_LOADED, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in loaded:
            out = dict(r)
            # represent SQL NULL as empty, BOOLEAN as lowercase literal
            for k in cols:
                if out[k] is None:
                    out[k] = ""
            if isinstance(r["delivered"], bool):
                out["delivered"] = "true" if r["delivered"] else "false"
            w.writerow(out)

    with open(OUT_REJECTED, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["source_row", "reason", "raw_record"])
        for r in rejected:
            w.writerow([r["row"], r["reason"], "|".join(r["raw"])])

    # review_required: the meaning-changing / unconfirmed decisions
    if counters["weight_sentinel_nulled"]:
        review_required.append({
            "code": "weight-sentinel",
            "detail": f"{counters['weight_sentinel_nulled']} row(s) had "
                      f"weight_kg=9999 treated as missing and loaded as NULL. "
                      f"Confirm 9999 is a sentinel, not a real weight.",
        })
    review_required.append({
        "code": "date-format-ambiguous",
        "detail": f"All dates parsed as {DATE_FORMAT_LABEL}. Values are "
                  f"undecidable from data (all components <= 12). Confirm the "
                  f"partner's convention; re-run with %d/%m/%Y if DD/MM/YYYY.",
    })

    summary = {
        "source": os.path.relpath(SRC, HERE),
        "target_table": "shipments",
        "date_format_used": DATE_FORMAT_LABEL,
        "counters": counters,
        "warnings": warnings,
        "review_required": review_required,
        "rejected_rows": rejected,
        "outputs": {
            "loaded_csv": os.path.relpath(OUT_LOADED, HERE),
            "rejected_csv": os.path.relpath(OUT_REJECTED, HERE),
        },
    }
    with open(OUT_SUMMARY, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    # ---- console report -----------------------------------------------------
    print("=" * 68)
    print("ETL run: partner_feed.csv -> shipments")
    print("=" * 68)
    print(f"  rows read (data)          : {counters['rows_read']}")
    print(f"  rows loaded               : {counters['rows_loaded']}")
    print(f"  rows rejected             : {counters['rows_rejected']}")
    print("  --- auto-fixes applied (counted) ---")
    print(f"  weight whitespace trimmed : {counters['weight_whitespace_trimmed']}")
    print(f"  weight 9999 -> NULL       : {counters['weight_sentinel_nulled']}")
    print(f"  carrier case canonicalized: {counters['carrier_canonicalized']}")
    print(f"  carrier unknown (kept)    : {counters['carrier_unknown']}")
    if rejected:
        print("  --- rejected rows ---")
        for r in rejected:
            print(f"    row {r['row']}: {r['reason']}")
    if review_required:
        print("  --- REVIEW REQUIRED (unconfirmed decisions) ---")
        for item in review_required:
            print(f"    [{item['code']}] {item['detail']}")
    print("=" * 68)
    print(f"  outputs: {os.path.basename(OUT_LOADED)}, "
          f"{os.path.basename(OUT_REJECTED)}, {os.path.basename(OUT_SUMMARY)}")

    # Exit codes: 0 clean, 2 completed with rejects/review, 3 fatal.
    if counters["rows_rejected"] or review_required:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
