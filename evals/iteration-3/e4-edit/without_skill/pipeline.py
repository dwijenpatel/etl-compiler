#!/usr/bin/env python3
"""pipeline.py — generated from vendor_orders.etlspec.yaml (etlspec 0.1).

Thin orchestration over etl_runtime. Every edge-case decision below is copied
verbatim from the spec; none are re-decided here.

Spec decisions encoded:
  order_id     TYP-07 -> string (not nullable)
  customer_name             string (not nullable)
  amount       TYP-01 -> to_decimal: strip $ and thousands ',', (n)=negative, scale 2
  order_date   TYP-03 -> to_date DMY %d/%m/%Y ; sentinel "N/A" -> null
  is_active    TYP-06 -> to_bool {Y:true, N:false}
  postal_code  TYP-07 -> string, max_length 10

Policies: NFC, strip control chars, normalize unicode whitespace, trim,
empty_string_is_null=true, error_disposition=quarantine,
error_budget={percent:25, min_rows:2}, duplicate_rows=keep, datetime=iso8601.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone

import etl_runtime as rt
from etl_runtime import NULL, CellError, FixLog

# --- spec-derived constants -------------------------------------------------
SPEC_NAME = "vendor_orders"
TAXONOMY_VERSION = "0.1"

DIALECT = {"delimiter": ",", "quotechar": '"'}
EXPECTED_COLUMNS = ["order_id", "cust_name", "amt", "dt", "active", "zip"]

POLICIES = {
    "unicode_normalization": "NFC",
    "strip_control_chars": True,
    "normalize_unicode_whitespace": True,
    "trim_whitespace": True,
    "empty_string_is_null": True,
}

ERROR_BUDGET = {"percent": 25, "min_rows": 2}

TARGET_HEADER = [
    "order_id",
    "customer_name",
    "amount",
    "order_date",
    "is_active",
    "postal_code",
]


def transform_row(src: dict, fixlog: FixLog):
    """Map one source record to a target record. Raises CellError on failure.

    Collects ALL cell errors for the row before raising, so a quarantined row
    reports every problem at once.
    """
    errors = []
    out = {}

    def clean(col):
        return rt.clean_string(src.get(col), col, POLICIES, fixlog)

    def guard(target, fn):
        try:
            out[target] = fn()
        except CellError as e:
            errors.append({"column": target, "code": e.code, "message": e.message})
            out[target] = NULL

    # order_id : string, not nullable  (TYP-07)
    guard("order_id", lambda: rt.to_string(clean("order_id"), "order_id"))
    # customer_name : string, not nullable
    guard("customer_name", lambda: rt.to_string(clean("cust_name"), "customer_name"))
    # amount : decimal(scale 2)  (TYP-01)
    guard(
        "amount",
        lambda: rt.to_decimal(
            clean("amt"), "amount", fixlog,
            thousands_sep=",", currency=True, accounting_negative=True, scale=2,
        ),
    )
    # order_date : date, DMY, sentinel N/A -> null  (TYP-03)
    guard(
        "order_date",
        lambda: rt.to_date(
            clean("dt"), "order_date", fixlog,
            formats=["%d/%m/%Y"], sentinels=["N/A"],
        ),
    )
    # is_active : boolean  (TYP-06)
    guard(
        "is_active",
        lambda: rt.to_bool(clean("active"), "is_active", mapping={"Y": True, "N": False}),
    )
    # postal_code : string, max_length 10  (TYP-07)
    guard(
        "postal_code",
        lambda: rt.to_string(clean("zip"), "postal_code", max_length=10),
    )

    # Non-nullable constraint enforcement.
    for col in ("order_id", "customer_name"):
        if out.get(col) is NULL and not any(e["column"] == col for e in errors):
            errors.append({
                "column": col,
                "code": "NUL-CONSTRAINT",
                "message": f"{col} is not nullable but produced null",
            })

    if errors:
        raise _RowError(errors)
    return out


class _RowError(Exception):
    def __init__(self, errors):
        self.errors = errors


def run(input_path: str, out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    fixlog = FixLog()

    with open(input_path, "rb") as fh:
        raw_bytes = fh.read()
    input_sha256 = hashlib.sha256(raw_bytes).hexdigest()

    text = raw_bytes.decode("utf-8")
    reader = csv.reader(text.splitlines(), delimiter=DIALECT["delimiter"],
                        quotechar=DIALECT["quotechar"])
    rows = list(reader)
    if not rows:
        raise SystemExit("empty input")
    header = rows[0]
    data_rows = rows[1:]

    good = []
    quarantined = []  # list of {"row": n, "raw": [...], "errors": [...]}
    error_tally = {}

    for i, values in enumerate(data_rows, start=2):  # start=2: line 1 is header
        if len(values) != len(header):
            rec = {
                "row": i,
                "raw": values,
                "errors": [{
                    "column": None,
                    "code": "ROW-RAGGED",
                    "message": f"expected {len(header)} fields, got {len(values)}",
                }],
            }
            quarantined.append(rec)
            error_tally["ROW-RAGGED"] = error_tally.get("ROW-RAGGED", 0) + 1
            continue

        src = dict(zip(header, values))
        try:
            out = transform_row(src, fixlog)
            good.append(out)
        except _RowError as e:
            quarantined.append({"row": i, "raw": values, "errors": e.errors})
            for err in e.errors:
                error_tally[err["code"]] = error_tally.get(err["code"], 0) + 1

    # --- error budget -------------------------------------------------------
    total = len(data_rows)
    budget = max(ERROR_BUDGET["min_rows"], math.ceil(ERROR_BUDGET["percent"] / 100 * total))
    over_budget = len(quarantined) > budget

    # --- write outputs ------------------------------------------------------
    out_csv = os.path.join(out_dir, f"{SPEC_NAME}.output.csv")
    tmp_csv = out_csv + ".tmp"
    with open(tmp_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(TARGET_HEADER)
        for r in good:
            w.writerow([rt.render(r[c]) for c in TARGET_HEADER])
    os.replace(tmp_csv, out_csv)  # atomic

    out_err = os.path.join(out_dir, f"{SPEC_NAME}.errors.jsonl")
    with open(out_err, "w", encoding="utf-8") as fh:
        for rec in quarantined:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    if over_budget:
        status, exit_code = "failed-error-budget", 1
    elif quarantined:
        status, exit_code = "completed-with-quarantine", 2
    else:
        status, exit_code = "completed", 0

    manifest = {
        "spec": SPEC_NAME,
        "taxonomy_version": TAXONOMY_VERSION,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "input": {"path": os.path.abspath(input_path), "sha256": input_sha256},
        "rows": {
            "read": total,
            "written": len(good),
            "quarantined": len(quarantined),
        },
        "error_budget": {**ERROR_BUDGET, "limit_rows": budget, "over_budget": over_budget},
        "errors_by_code": error_tally,
        "auto_fixes": fixlog.as_list(),
        "outputs": {"data": os.path.basename(out_csv), "errors": os.path.basename(out_err)},
        "status": status,
        "exit_code": exit_code,
    }
    out_manifest = os.path.join(out_dir, f"{SPEC_NAME}.manifest.json")
    with open(out_manifest, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)

    # human-readable run summary
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    inp = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        here, "eval-inputs", "vendor_orders_sample.csv")
    outd = sys.argv[2] if len(sys.argv) > 2 else os.path.join(here, "out")
    sys.exit(run(inp, outd))
