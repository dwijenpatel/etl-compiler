"""Smoke-test pipeline in exactly the shape codegen would produce.

Run from this directory:  python3 smoke_pipeline.py
Expected: exit prints 2 (completed with quarantined rows); etl_out/ contains
output.csv (6 rows), quarantine.csv (ragged row 7), errors.jsonl, summary.json, manifest.json.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "skill", "etl-generator", "assets"))
import etl_runtime as rt

CONFIG = {
    "name": "messy_smoke_test",
    "encoding": "utf-8",
    "delimiter": ",",
    "expected_columns": ["order_id", "customer", "amt", "order_date", "active", "zip", "notes"],
    "output_columns": ["order_id", "customer_name", "amount", "order_date", "is_active", "postal_code", "notes"],
    "policies": {
        "unicode_normalization": "NFC", "strip_control_chars": True,
        "normalize_unicode_whitespace": True, "trim_whitespace": True,
        "empty_string_is_null": True,
        "sentinels": {"notes": ["N/A"]},                       # NUL-03 detected-confirmed
        "error_disposition": "quarantine",                     # ERR-01 default
        "error_budget": {"percent": 50, "min_rows": 3},        # loosened for tiny sample
    },
}

def transform_row(row, report):
    # STR-06: footer rows confirmed excluded (explicit decision)
    if row["order_id"] and not row["order_id"].isdigit():
        raise rt.SkipRow("STR-06", "footer/total row")
    out = {}
    out["order_id"] = rt.not_null(row["order_id"], "order_id")              # TYP-07: string
    out["customer_name"] = rt.not_null(row["customer"], "customer")
    out["amount"] = rt.to_decimal(row["amt"], "amt", thousands_sep=",",
                                  currency=True, accounting_negative=True, scale=2)  # TYP-01
    out["order_date"] = rt.to_date(row["order_date"], "order_date", formats=["%m/%d/%Y"])  # TYP-03: MDY (explicit)
    out["is_active"] = rt.to_bool(row["active"], "active", mapping={"Y": True, "N": False})  # TYP-06
    out["postal_code"] = rt.check_length(row["zip"], "zip", max_length=10)  # TYP-07: string, TYP-11
    out["notes"] = row["notes"]
    return out

result = rt.run_pipeline(input_path=os.path.join(os.path.dirname(__file__) or ".", "messy_sample.csv"), out_dir="./etl_out",
                         config=CONFIG, transform_row=transform_row)
print("exit_code:", result.exit_code)
