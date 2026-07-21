#!/usr/bin/env python3
"""orders_export pipeline — generated from orders_export.etlspec.yaml.

GENERATION NOTE
    This pipeline is HAND-GENERATED per the skill's documented fallback
    (references/codegen-guide.md), not emitted by scripts/compile_spec.py. The spec
    validates and compiles, but two of its behaviors exceed the current deterministic
    compiler's vocabulary, so its emitted pipeline would be incorrect:
      * ENC-06 mojibake repair WITH ERR-04 counting — the compiler's `expr` escape
        hatch passes helper functions only `row`, never `report`, so the emitted
        `_expr_customer_name(row)` references an undefined `report` (NameError).
      * STR-06 footer/total-row skip — the compiler has no row-filter emission.
    Both behaviors are recorded in the spec (the source of truth). Regenerating this
    file byte-deterministically will require the runtime/compiler to gain an ENC-06
    op with report access and a row-filter step (see the run report handed back).

CONTRACT
    All edge-case semantics live in etl_runtime.py; this file stays thin orchestration.
    Error codes in reports are ETL Failure-Mode Taxonomy v0.2 IDs.
    Edit the spec and regenerate rather than hand-editing transform logic here.
"""
import argparse

import etl_runtime as rt

# ---- Resolved configuration (from orders_export.etlspec.yaml — edit the spec, not this) ----
CONFIG = {
    "name": "orders_export",
    "spec_version": "0.1",
    "generator_version": "etl-generator/0.2 (hand-gen fallback; runtime 0.2.0 + repair_mojibake)",
    "encoding": "utf-8",                                   # ENC-01
    "delimiter": ",",
    "quotechar": '"',
    "expected_columns": ["order_id", "customer", "amt", "order_date", "active", "zip", "notes"],
    "output_columns": ["order_id", "customer_name", "amount", "order_date", "is_active", "postal_code", "notes"],
    "policies": {
        "unicode_normalization": "NFC",          # ENC-03
        "strip_control_chars": True,             # ENC-04
        "normalize_unicode_whitespace": True,    # ENC-05
        "trim_whitespace": True,                 # NUL-02
        "empty_string_is_null": True,            # NUL-01
        "null_propagation": "sql",               # NUL-05
        "datetime_rendering": "iso8601",         # TYP-05
        "error_disposition": "quarantine",       # ERR-01
        "error_budget": {"percent": 5, "min_rows": 100},   # ERR-02
        "duplicate_rows": "keep",                # STR-05: keep exact dups, but report them
        "sentinels": {"notes": ["N/A"]},         # NUL-03: applied during null resolution
    },
}


def transform_row(row, report):
    """Map one cleaned input row -> output row. Values arrive text-cleaned and
    null-resolved per CONFIG policies (ENC-03/04/05, NUL-01/02/03), applied and
    counted by the runtime before this function is called. Raise rt.RowError to
    reject a row; raise rt.SkipRow to exclude it per an explicit spec decision.
    """
    # STR-06: exclude the confirmed footer/total row (spec row_filters; profiler
    # flagged the final 'Total,,3222.56,,,,' row). Counted as a warning, never silent.
    if row.get("order_id") == "Total":
        raise rt.SkipRow("STR-06", "footer/total row")

    out = {}
    # order_id <- order_id   [TYP-07: kept as string, uniform 8-digit — detected-confirmed]
    out["order_id"] = rt.not_null(row["order_id"], "order_id")
    # customer_name <- customer   [ENC-06: opt-in mojibake repair, counted — confirmed by target sample]
    out["customer_name"] = rt.not_null(
        rt.repair_mojibake(row["customer"], "customer", report), "customer")
    # amount <- amt   [TYP-01: strip $/commas, (n)=negative; scale 2 (TYP-08) — confirmed by target]
    out["amount"] = rt.to_decimal(row["amt"], "amt", thousands_sep=",",
                                  currency=True, accounting_negative=True, scale=2)
    # order_date <- order_date   [TYP-03: MDY — confirmed by target sample]
    out["order_date"] = rt.to_date(row["order_date"], "order_date", formats=["%m/%d/%Y"])
    # is_active <- active   [TYP-06: Y/N vocabulary — confirmed by target sample]
    out["is_active"] = rt.to_bool(row["active"], "active",
                                  mapping={"Y": True, "N": False}, report=report)
    # postal_code <- zip   [TYP-07: string, leading zeros; TYP-11: max_length 10]
    out["postal_code"] = rt.check_length(row["zip"], "zip", max_length=10)
    # notes <- notes   [NUL-03: 'N/A' sentinel -> null, resolved by the runtime before this point]
    out["notes"] = row["notes"]
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input")
    p.add_argument("--out-dir", default="./etl_out")
    args = p.parse_args()
    result = rt.run_pipeline(input_path=args.input, out_dir=args.out_dir,
                             config=CONFIG, transform_row=transform_row)
    raise SystemExit(result.exit_code)


if __name__ == "__main__":
    main()
