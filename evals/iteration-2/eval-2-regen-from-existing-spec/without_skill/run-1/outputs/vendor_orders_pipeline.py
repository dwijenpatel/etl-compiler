#!/usr/bin/env python3
"""vendor_orders_pipeline.py — generated from vendor_orders.etlspec.yaml (etlspec 0.1).

Thin orchestration over etl_runtime. Every edge-case decision below is transcribed
from the spec's recorded decisions/policies; nothing is improvised here.

Spec provenance transcribed:
  policies.empty_string_is_null      explicit
  policies.error_disposition         default  -> quarantine
  policies.error_budget              explicit -> {percent: 25, min_rows: 2}
  policies.duplicate_rows            default  -> keep
  amount  TYP-01 strip $/commas; (n)=negative   detected-confirmed
  order_date TYP-03 MDY (%m/%d/%Y)              explicit; sentinel N/A -> null
  is_active  TYP-06 Y/N vocabulary              detected-confirmed
  order_id/postal_code TYP-07 string            detected-confirmed

Exit codes: 0 = completed clean, 2 = completed with quarantine (within budget),
            1 = run error (budget exceeded or fatal).
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

import etl_runtime as rt

SPEC_NAME = "vendor_orders"
TAXONOMY_VERSION = "0.1"

# --- policies (from spec.policies) -----------------------------------------
POLICIES = dict(
    unicode_normalization="NFC",
    strip_control_chars=True,
    normalize_unicode_whitespace=True,
    trim_whitespace=True,
    empty_string_is_null=True,
)
ERROR_DISPOSITION = "quarantine"
ERROR_BUDGET = {"percent": 25, "min_rows": 2}
DUPLICATE_ROWS = "keep"

EXPECTED_COLUMNS = ["order_id", "cust_name", "amt", "dt", "active", "zip"]
TARGET_COLUMNS = ["order_id", "customer_name", "amount", "order_date", "is_active", "postal_code"]


def transform_row(raw: dict, fixes: rt.FixCounter) -> dict:
    """Apply all six mappings to one raw source row. Raises rt.TransformError on any
    row-level failure (caller quarantines the whole row)."""
    out: dict = {}

    # order_id  <- order_id   (TYP-07 string; nullable: false)
    v, fx = rt.normalize_cell(raw.get("order_id"), **POLICIES)
    fixes.add("order_id", fx)
    out["order_id"] = rt.enforce_string(v, nullable=False, column="order_id")

    # customer_name <- cust_name  (nullable: false)
    v, fx = rt.normalize_cell(raw.get("cust_name"), **POLICIES)
    fixes.add("customer_name", fx)
    out["customer_name"] = rt.enforce_string(v, nullable=False, column="customer_name")

    # amount <- amt  (TYP-01: strip $ and commas; (n)=negative; decimal scale 2; nullable)
    v, fx = rt.normalize_cell(raw.get("amt"), **POLICIES)
    fixes.add("amount", fx)
    d, fx2 = rt.to_decimal(v, scale=2, thousands_sep=",", currency=True, accounting_negative=True)
    fixes.add("amount", fx2)
    out["amount"] = rt.enforce_not_null(d, nullable=True, column="amount")

    # order_date <- dt  (TYP-03 MDY; sentinel N/A -> null; nullable)
    v, fx = rt.normalize_cell(raw.get("dt"), **POLICIES)
    fixes.add("order_date", fx)
    dt, fx2 = rt.to_date(v, formats=["%m/%d/%Y"], sentinels=["N/A"])
    fixes.add("order_date", fx2)
    out["order_date"] = rt.enforce_not_null(dt, nullable=True, column="order_date")

    # is_active <- active  (TYP-06 Y/N; nullable)
    v, fx = rt.normalize_cell(raw.get("active"), **POLICIES)
    fixes.add("is_active", fx)
    b, fx2 = rt.to_bool(v, mapping={"Y": True, "N": False})
    fixes.add("is_active", fx2)
    out["is_active"] = rt.enforce_not_null(b, nullable=True, column="is_active")

    # postal_code <- zip  (TYP-07 string; max_length 10; nullable)
    v, fx = rt.normalize_cell(raw.get("zip"), **POLICIES)
    fixes.add("postal_code", fx)
    out["postal_code"] = rt.enforce_string(v, nullable=True, max_length=10, column="postal_code")

    return out


def run(input_path: str, out_dir: str) -> int:
    base = os.path.join(out_dir, SPEC_NAME)
    output_path = base + "_output.csv"
    errors_path = base + "_errors.jsonl"
    manifest_path = base + "_manifest.json"

    fixes = rt.FixCounter()
    error_type_agg: dict[str, int] = {}
    error_records: list[dict] = []
    clean_rows: list[dict] = []
    rows_read = 0

    with open(input_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        if header != EXPECTED_COLUMNS:
            # KEY-01: header mismatch is a run-level (fatal) error.
            sys.stderr.write(
                f"KEY-01 header mismatch: expected {EXPECTED_COLUMNS}, got {header}\n")
            return 1

        for i, raw in enumerate(reader, start=1):
            rows_read += 1
            # KEY-02: ragged row (field count != header) -> quarantine.
            if raw.get(None) is not None or any(v is None for v in raw.values()):
                code = "KEY-02"
                error_type_agg[code] = error_type_agg.get(code, 0) + 1
                error_records.append({
                    "row": i, "code": code,
                    "message": "ragged row: field count does not match header",
                    "raw": raw,
                })
                continue
            try:
                clean_rows.append(transform_row(raw, fixes))
            except rt.TransformError as e:
                error_type_agg[e.code] = error_type_agg.get(e.code, 0) + 1
                error_records.append({
                    "row": i, "code": e.code, "message": e.message, "raw": raw,
                })

    rows_quarantined = len(error_records)
    rows_written = len(clean_rows)

    # --- ERR-01 error budget ------------------------------------------------
    threshold = rt.budget_threshold(rows_read, **ERROR_BUDGET)
    budget_exceeded = rows_quarantined > threshold

    # --- atomic write of clean output ---------------------------------------
    _atomic_write_csv(output_path, TARGET_COLUMNS, clean_rows)

    # --- per-row error records (ERR-03 granularity 1) -----------------------
    with open(errors_path, "w", encoding="utf-8") as fh:
        for rec in error_records:
            fh.write(json.dumps(rec) + "\n")

    # --- manifest: aggregates + run summary (ERR-03 granularities 2 & 3) ----
    if budget_exceeded:
        status = "failed_budget_exceeded"
    elif rows_quarantined:
        status = "completed_with_quarantine"
    else:
        status = "completed_clean"

    manifest = {
        "pipeline": SPEC_NAME,
        "taxonomy_version": TAXONOMY_VERSION,
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "input": os.path.abspath(input_path),
        "output": os.path.abspath(output_path),
        "errors_file": os.path.abspath(errors_path),
        "rows_read": rows_read,
        "rows_written": rows_written,
        "rows_quarantined": rows_quarantined,
        "duplicate_rows_policy": DUPLICATE_ROWS,
        "error_disposition": ERROR_DISPOSITION,
        "error_budget": {**ERROR_BUDGET, "threshold": threshold, "exceeded": budget_exceeded},
        "auto_fixes": fixes.as_dict(),
        "auto_fixes_total": fixes.total(),
        "error_types": error_type_agg,
        "status": status,
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    # --- console run summary ------------------------------------------------
    print(f"[{SPEC_NAME}] status={status}")
    print(f"  rows_read={rows_read} written={rows_written} quarantined={rows_quarantined}")
    print(f"  error_budget threshold={threshold} exceeded={budget_exceeded}")
    print(f"  auto_fixes_total={fixes.total()} -> {json.dumps(fixes.as_dict())}")
    print(f"  error_types={json.dumps(error_type_agg)}")
    print(f"  output={output_path}")
    print(f"  manifest={manifest_path}")

    if budget_exceeded:
        return 1
    if rows_quarantined:
        return 2
    return 0


def _atomic_write_csv(path: str, columns: list[str], rows: list[dict]):
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(columns)
            for row in rows:
                w.writerow([rt.render(row[c]) for c in columns])
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


if __name__ == "__main__":
    inp = sys.argv[1] if len(sys.argv) > 1 else "eval-inputs/vendor_orders_sample.csv"
    outd = sys.argv[2] if len(sys.argv) > 2 else "out"
    sys.exit(run(inp, outd))
