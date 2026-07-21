#!/usr/bin/env python3
"""pipeline_vendor_orders.py — generated from vendor_orders.etlspec.yaml (etlspec 0.1).

Thin orchestration. All edge-case semantics live in etl_runtime. Every policy and mapping
decision below is transcribed verbatim from the spec; no decision is invented here.

Exit codes:
  0  clean run, no quarantined rows
  1  run error (error budget exceeded / fatal)
  2  completed with quarantined rows
"""

import csv
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone

import etl_runtime as rt

# --- transcribed from spec -------------------------------------------------

SOURCE_CSV = os.path.join(os.path.dirname(__file__), "eval-inputs", "vendor_orders_sample.csv")
OUT_DIR = os.path.join(os.path.dirname(__file__), "output")

EXPECTED_COLUMNS = ["order_id", "cust_name", "amt", "dt", "active", "zip"]

POLICIES = {
    "unicode_normalization": "NFC",
    "strip_control_chars": True,
    "normalize_unicode_whitespace": True,
    "trim_whitespace": True,
    "empty_string_is_null": True,
    "null_propagation": "sql",
    "datetime_rendering": "iso8601",
    "error_disposition": "quarantine",
    "error_budget": {"percent": 25, "min_rows": 2},
    "duplicate_rows": "keep",
}

TARGET_COLUMNS = [
    "order_id", "customer_name", "amount", "order_date", "is_active", "postal_code",
]

# nullability from the target schema (used to enforce non-null contracts)
NON_NULLABLE = {"order_id", "customer_name"}


def transform_row(raw, warn):
    """Map one source row dict -> target row dict. `warn(col, code)` tallies an auto-fix.
    Raises rt.CellError on any cell that cannot be coerced."""

    def clean(src_col):
        return rt.apply_string_policies(
            raw.get(src_col), POLICIES, lambda code: warn(src_col, code)
        )

    out = {}

    # order_id  <- order_id   (TYP-07 string)
    out["order_id"] = rt.to_string(clean("order_id"))

    # customer_name <- cust_name
    out["customer_name"] = rt.to_string(clean("cust_name"))

    # amount <- amt  (TYP-01: strip $ and commas; (n) = negative; decimal scale 2)
    out["amount"] = rt.to_decimal(
        clean("amt"), scale=2, thousands_sep=",", currency=True,
        accounting_negative=True, warn=lambda code: warn("amount", code),
    )

    # order_date <- dt  (sentinel N/A -> null; to_date MDY %m/%d/%Y; iso8601)
    dt_val = rt.apply_sentinels(clean("dt"), ["N/A"], warn=lambda code: warn("order_date", code))
    out["order_date"] = rt.to_date(dt_val, ["%m/%d/%Y"], rendering="iso8601")

    # is_active <- active  (TYP-06 Y/N vocabulary)
    out["is_active"] = rt.to_bool(clean("active"), {"Y": True, "N": False})

    # postal_code <- zip  (TYP-07 string, preserve leading zeros, max_length 10)
    out["postal_code"] = rt.to_string(clean("zip"), max_length=10)

    # enforce non-null contract for required target columns
    for col in NON_NULLABLE:
        if out[col] is None:
            raise rt.CellError("NUL-01", f"required column {col} is null")

    return out


def render_cell(value):
    """SQL null propagation: null renders as empty string. Bools as true/false."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    out_csv = os.path.join(OUT_DIR, "vendor_orders.csv")
    err_jsonl = os.path.join(OUT_DIR, "errors.jsonl")
    summary_json = os.path.join(OUT_DIR, "run_summary.json")

    clean_rows = []
    error_records = []           # ERR-03 granularity 1: per-row error records
    error_type_counts = Counter()  # ERR-03 granularity 2: per-error-type aggregate
    # ERR-04: auto-fixes counted per column per taxonomy ID
    autofix_counts = {}  # (column, code) -> int

    def warn(column, code):
        autofix_counts[(column, code)] = autofix_counts.get((column, code), 0) + 1

    total_rows = 0
    with open(SOURCE_CSV, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != EXPECTED_COLUMNS:
            print(f"FATAL: header {reader.fieldnames} != expected {EXPECTED_COLUMNS}",
                  file=sys.stderr)
            return 1
        for lineno, raw in enumerate(reader, start=2):  # line 1 is header
            total_rows += 1
            try:
                clean_rows.append(transform_row(raw, warn))
            except rt.CellError as e:
                error_type_counts[e.code] += 1
                error_records.append({
                    "source_line": lineno,
                    "code": e.code,
                    "message": e.message,
                    "raw": raw,  # preserve the raw row verbatim for reprocessing
                })

    quarantined = len(error_records)

    # --- error budget check (ERR: fail the run if too many rows quarantined) ---
    budget = POLICIES["error_budget"]
    budget_exceeded = False
    if total_rows:
        pct = 100.0 * quarantined / total_rows
        if quarantined >= budget["min_rows"] and pct > budget["percent"]:
            budget_exceeded = True

    # --- atomic write of clean output ---
    fd, tmp_path = tempfile.mkstemp(dir=OUT_DIR, suffix=".tmp")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(TARGET_COLUMNS)
        for row in clean_rows:
            writer.writerow([render_cell(row[c]) for c in TARGET_COLUMNS])
    os.replace(tmp_path, out_csv)

    # --- per-row error records ---
    with open(err_jsonl, "w", encoding="utf-8") as fh:
        for rec in error_records:
            fh.write(json.dumps(rec) + "\n")

    # --- run summary + manifest (ERR-03 granularity 3) ---
    summary = {
        "pipeline": "vendor_orders",
        "etlspec": "0.1",
        "taxonomy_version": "0.1",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "source": os.path.basename(SOURCE_CSV),
        "rows_in": total_rows,
        "rows_out": len(clean_rows),
        "rows_quarantined": quarantined,
        "error_budget": budget,
        "error_budget_exceeded": budget_exceeded,
        "error_type_aggregate": dict(error_type_counts),
        "autofixes": [
            {"column": col, "code": code, "count": n}
            for (col, code), n in sorted(autofix_counts.items())
        ],
        "autofixes_total": sum(autofix_counts.values()),
        "outputs": {
            "clean_csv": os.path.basename(out_csv),
            "errors_jsonl": os.path.basename(err_jsonl),
        },
    }
    with open(summary_json, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    # --- console report ---
    print(f"rows_in={total_rows} rows_out={len(clean_rows)} quarantined={quarantined}")
    if autofix_counts:
        print("auto-fixes:")
        for (col, code), n in sorted(autofix_counts.items()):
            print(f"  {col:<12} {code}  x{n}")
    if error_type_counts:
        print("errors by type:")
        for code, n in error_type_counts.items():
            print(f"  {code}  x{n}")

    if budget_exceeded:
        print("FATAL: error budget exceeded", file=sys.stderr)
        return 1
    return 2 if quarantined else 0


if __name__ == "__main__":
    sys.exit(main())
