#!/usr/bin/env python3
"""ETL pipeline: partner monthly shipment feed -> `shipments` table.

Target schema:
    CREATE TABLE shipments (
      shipment_id VARCHAR(12) NOT NULL,
      ship_date DATE,
      weight_kg DECIMAL(8,2),
      carrier VARCHAR(20),
      delivered BOOLEAN
    );

Design principles
-----------------
1. NO GUESSING on ambiguous data. The partner feed uses slash dates
   (e.g. "03/04/2026"). If the feed as a whole does not prove whether the
   format is MM/DD/YYYY or DD/MM/YYYY (i.e. no row has a component > 12),
   the pipeline will NOT pick one. Ambiguous dates are loaded as NULL and
   every affected row is logged in the issues report, so the load can be
   repaired with a single UPDATE (or a re-run) once the partner confirms
   the format via --date-format {MDY,DMY}.
2. Rows that would violate the schema (missing/oversized shipment_id) are
   quarantined, never dropped silently.
3. Every transformation and every skipped/NULLed value is recorded in a
   machine-readable run report.

Usage:
    python etl.py INPUT.csv --out-dir OUTDIR [--date-format {MDY,DMY,auto}]
                  [--weight-sentinels 9999] [--db PATH.db]

Exit codes: 0 = load completed (possibly with warnings, see report)
            1 = fatal error, nothing loaded
"""

import argparse
import csv
import json
import sqlite3
import sys
from datetime import date
from decimal import Decimal, InvalidOperation

TARGET_COLUMNS = ["shipment_id", "ship_date", "weight_kg", "carrier", "delivered"]

# Header mapping: partner feed column -> target column
SOURCE_TO_TARGET = {
    "ship_id": "shipment_id",
    "date": "ship_date",
    "wt": "weight_kg",
    "carrier": "carrier",
    "status": "delivered",
}

# Canonical carrier names (case-insensitive match on the feed value).
CARRIER_CANONICAL = {"ups": "UPS", "fedex": "FedEx", "dhl": "DHL", "usps": "USPS"}

TRUTHY = {"y", "yes", "true", "t", "1"}
FALSY = {"n", "no", "false", "f", "0"}

MAX_WEIGHT = Decimal("999999.99")  # DECIMAL(8,2) upper bound


def parse_slash_date(raw):
    """Split 'a/b/yyyy' into ints. Returns (a, b, year) or None."""
    parts = raw.strip().split("/")
    if len(parts) != 3:
        return None
    try:
        a, b, y = (int(p) for p in parts)
    except ValueError:
        return None
    if y < 100:  # 2-digit years are themselves ambiguous; refuse
        return None
    return a, b, y


def feasible_formats(a, b, y):
    """Which interpretations of a/b/y are valid calendar dates?"""
    out = set()
    try:
        date(y, a, b)
        out.add("MDY")
    except ValueError:
        pass
    try:
        date(y, b, a)
        out.add("DMY")
    except ValueError:
        pass
    return out


def infer_date_format(raw_dates, issues):
    """Determine the feed's date format ONLY if the data proves it.

    Returns 'MDY', 'DMY', or None (ambiguous / undeterminable).
    """
    candidates = {"MDY", "DMY"}
    for row_no, raw in raw_dates:
        parsed = parse_slash_date(raw)
        if parsed is None:
            continue  # unparseable values reported per-row later
        feas = feasible_formats(*parsed)
        if feas:
            candidates &= feas
    if len(candidates) == 1:
        return candidates.pop()
    if not candidates:
        issues.append({
            "row": None, "field": "ship_date", "value": None,
            "issue": "date_format_contradiction",
            "detail": "No single format (MDY or DMY) fits all rows; feed is internally inconsistent.",
        })
    return None


def transform_row(row_no, raw, date_format, weight_sentinels, issues):
    """Transform one raw CSV row. Returns (record, reject_reason).

    record is a dict of target columns (only when reject_reason is None).
    """
    get = lambda k: (raw.get(k) or "").strip()

    # --- shipment_id: VARCHAR(12) NOT NULL ---
    shipment_id = get("ship_id")
    if not shipment_id:
        return None, "shipment_id is missing but target column is NOT NULL"
    if len(shipment_id) > 12:
        return None, f"shipment_id '{shipment_id}' exceeds VARCHAR(12)"

    # --- ship_date: DATE (nullable) ---
    ship_date = None
    raw_date = get("date")
    if raw_date:
        parsed = parse_slash_date(raw_date)
        if parsed is None:
            issues.append({"row": row_no, "field": "ship_date", "value": raw_date,
                           "issue": "unparseable_date",
                           "detail": "Not a/b/yyyy with 4-digit year; loaded as NULL."})
        else:
            a, b, y = parsed
            feas = feasible_formats(a, b, y)
            if date_format in feas:
                m, d = (a, b) if date_format == "MDY" else (b, a)
                ship_date = date(y, m, d)
            elif len(feas) == 1:
                # Only one interpretation is a real date -> unambiguous even
                # without a global format.
                fmt = next(iter(feas))
                m, d = (a, b) if fmt == "MDY" else (b, a)
                ship_date = date(y, m, d)
            elif a == b and feas:
                ship_date = date(y, a, b)  # same date either way
            else:
                issues.append({"row": row_no, "field": "ship_date", "value": raw_date,
                               "issue": "ambiguous_date_not_guessed",
                               "detail": "Valid as both MM/DD/YYYY and DD/MM/YYYY; "
                                         "loaded as NULL pending partner confirmation."})

    # --- weight_kg: DECIMAL(8,2) (nullable) ---
    weight = None
    raw_wt = get("wt")
    if raw_wt:
        try:
            w = Decimal(raw_wt)
        except InvalidOperation:
            w = None
            issues.append({"row": row_no, "field": "weight_kg", "value": raw_wt,
                           "issue": "unparseable_weight", "detail": "Loaded as NULL."})
        if w is not None:
            if w in weight_sentinels:
                issues.append({"row": row_no, "field": "weight_kg", "value": raw_wt,
                               "issue": "sentinel_weight_nulled",
                               "detail": f"Value {w} treated as missing-data sentinel; loaded as NULL. "
                                         "Re-run with --weight-sentinels '' to keep literal values."})
            elif w < 0:
                issues.append({"row": row_no, "field": "weight_kg", "value": raw_wt,
                               "issue": "negative_weight_nulled", "detail": "Loaded as NULL."})
            elif w > MAX_WEIGHT:
                issues.append({"row": row_no, "field": "weight_kg", "value": raw_wt,
                               "issue": "weight_overflow_nulled",
                               "detail": "Exceeds DECIMAL(8,2); loaded as NULL."})
            else:
                q = w.quantize(Decimal("0.01"))
                if q != w:
                    issues.append({"row": row_no, "field": "weight_kg", "value": raw_wt,
                                   "issue": "weight_rounded",
                                   "detail": f"Rounded to 2 decimal places: {q}."})
                weight = q

    # --- carrier: VARCHAR(20) (nullable) ---
    carrier = None
    raw_carrier = get("carrier")
    if raw_carrier:
        canonical = CARRIER_CANONICAL.get(raw_carrier.lower())
        if canonical is not None:
            carrier = canonical
            if raw_carrier != canonical:
                issues.append({"row": row_no, "field": "carrier", "value": raw_carrier,
                               "issue": "carrier_normalized", "detail": f"Normalized to '{canonical}'."})
        else:
            carrier = raw_carrier[:20]
            issues.append({"row": row_no, "field": "carrier", "value": raw_carrier,
                           "issue": "carrier_unrecognized",
                           "detail": "Not in canonical list; loaded as-is"
                                     + (" (truncated to 20 chars)." if len(raw_carrier) > 20 else ".")})

    # --- delivered: BOOLEAN (nullable) ---
    delivered = None
    raw_status = get("status")
    if raw_status:
        low = raw_status.lower()
        if low in TRUTHY:
            delivered = True
        elif low in FALSY:
            delivered = False
        else:
            issues.append({"row": row_no, "field": "delivered", "value": raw_status,
                           "issue": "unparseable_status", "detail": "Loaded as NULL."})

    return {"shipment_id": shipment_id, "ship_date": ship_date,
            "weight_kg": weight, "carrier": carrier, "delivered": delivered}, None


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input_csv")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--date-format", choices=["MDY", "DMY", "auto"], default="auto",
                    help="Slash-date interpretation. 'auto' infers it only when the data "
                         "proves it; otherwise ambiguous dates load as NULL (default: auto).")
    ap.add_argument("--weight-sentinels", default="9999",
                    help="Comma-separated numeric values treated as missing weight "
                         "(default: 9999). Pass '' to disable.")
    ap.add_argument("--db", default=None, help="Optional SQLite file to load into (demo target).")
    args = ap.parse_args(argv)

    import os
    os.makedirs(args.out_dir, exist_ok=True)

    weight_sentinels = set()
    if args.weight_sentinels.strip():
        weight_sentinels = {Decimal(v.strip()) for v in args.weight_sentinels.split(",") if v.strip()}

    with open(args.input_csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in SOURCE_TO_TARGET if c not in header]
        if missing:
            print(f"FATAL: input is missing expected column(s): {missing}. "
                  f"Found header: {header}", file=sys.stderr)
            return 1
        raw_rows = []
        blank_rows = 0
        for i, row in enumerate(reader, start=2):  # 1-based file line numbers; header = line 1
            if not any((v or "").strip() for v in row.values()):
                blank_rows += 1
                continue
            raw_rows.append((i, {(k or "").strip(): v for k, v in row.items()}))

    issues = []

    # Resolve the date format without guessing.
    date_format = args.date_format
    inferred = None
    if date_format == "auto":
        inferred = infer_date_format([(n, (r.get("date") or "")) for n, r in raw_rows], issues)
        date_format = inferred  # may be None -> per-row ambiguity handling

    records, rejects = [], []
    seen_ids = {}
    for row_no, raw in raw_rows:
        rec, reason = transform_row(row_no, raw, date_format, weight_sentinels, issues)
        if reason is not None:
            rejects.append({"row": row_no, "reason": reason, **raw})
            continue
        sid = rec["shipment_id"]
        if sid in seen_ids:
            issues.append({"row": row_no, "field": "shipment_id", "value": sid,
                           "issue": "duplicate_shipment_id",
                           "detail": f"Also appears on row {seen_ids[sid]}; both loaded (no PK on target)."})
        else:
            seen_ids[sid] = row_no
        records.append(rec)

    # ---- Write load-ready CSV (mirrors target table; empty cell = NULL) ----
    def fmt(v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, date):
            return v.isoformat()
        return str(v)

    loaded_csv = os.path.join(args.out_dir, "shipments_loaded.csv")
    with open(loaded_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(TARGET_COLUMNS)
        for rec in records:
            w.writerow([fmt(rec[c]) for c in TARGET_COLUMNS])

    # ---- Quarantine ----
    quarantine_csv = os.path.join(args.out_dir, "quarantine.csv")
    with open(quarantine_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source_row", "reject_reason", "ship_id", "date", "wt", "carrier", "status"])
        for r in rejects:
            w.writerow([r["row"], r["reason"], r.get("ship_id", ""), r.get("date", ""),
                        r.get("wt", ""), r.get("carrier", ""), r.get("status", "")])

    # ---- Optional real load into SQLite (demonstrates the INSERTs) ----
    if args.db:
        conn = sqlite3.connect(args.db)
        conn.execute("DROP TABLE IF EXISTS shipments")
        conn.execute("""
            CREATE TABLE shipments (
              shipment_id VARCHAR(12) NOT NULL,
              ship_date DATE,
              weight_kg DECIMAL(8,2),
              carrier VARCHAR(20),
              delivered BOOLEAN
            )""")
        conn.executemany(
            "INSERT INTO shipments (shipment_id, ship_date, weight_kg, carrier, delivered) "
            "VALUES (?, ?, ?, ?, ?)",
            [(r["shipment_id"],
              r["ship_date"].isoformat() if r["ship_date"] else None,
              float(r["weight_kg"]) if r["weight_kg"] is not None else None,
              r["carrier"],
              r["delivered"]) for r in records])
        conn.commit()
        conn.close()

    # ---- Run report ----
    ambiguous = [i for i in issues if i["issue"] == "ambiguous_date_not_guessed"]
    report = {
        "input": os.path.abspath(args.input_csv),
        "rows_read": len(raw_rows),
        "blank_rows_skipped": blank_rows,
        "rows_loaded": len(records),
        "rows_quarantined": len(rejects),
        "date_format_setting": args.date_format,
        "date_format_used": date_format,
        "date_format_inference": (
            None if args.date_format != "auto" else
            (f"proven {inferred} by data" if inferred else
             "UNRESOLVED: every date is valid as both MM/DD/YYYY and DD/MM/YYYY; "
             "no format was assumed")),
        "weight_sentinels": sorted(str(s) for s in weight_sentinels),
        "issues": issues,
        "quarantined": rejects,
    }
    report_path = os.path.join(args.out_dir, "run_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    # ---- Console summary ----
    print(f"Rows read: {len(raw_rows)} (+{blank_rows} blank skipped) | "
          f"loaded: {len(records)} | quarantined: {len(rejects)} | issues: {len(issues)}")
    if args.date_format == "auto" and inferred is None:
        print("WARNING: date format is AMBIGUOUS (MM/DD vs DD/MM cannot be determined "
              f"from this feed). {len(ambiguous)} ship_date value(s) loaded as NULL. "
              "Confirm the format with the partner, then re-run with --date-format MDY "
              "or --date-format DMY.")
    for r in rejects:
        print(f"QUARANTINED row {r['row']}: {r['reason']}")
    print(f"Outputs: {loaded_csv}, {quarantine_csv}, {report_path}"
          + (f", {args.db}" if args.db else ""))
    return 0


if __name__ == "__main__":
    sys.exit(run())
