#!/usr/bin/env python3
"""vendor_orders_pipeline.py

GENERATED from eval-inputs/vendor_orders.etlspec.yaml (etlspec 0.1,
taxonomy_version 0.1). Thin orchestration only: all edge-case semantics live in
etl_runtime.py. Do not hand-edit decisions here — change the spec and regenerate.

Every policy/decision below is copied verbatim from the spec so the spec remains
the single source of truth. Taxonomy IDs appear in comments at each decision.

Exit status:
  0  clean run, no quarantines
  2  completed with quarantines, within error budget
  1  failed: error budget exceeded (or fatal setup error)
"""

import csv
import os
import sys

import etl_runtime as rt


# --------------------------------------------------------------------------- #
# Spec, transcribed. Source of truth is the .etlspec.yaml.
# --------------------------------------------------------------------------- #

SPEC_NAME = "vendor_orders"
TAXONOMY_VERSION = "0.1"

SOURCE = {
    "encoding": "utf-8",                       # ENC: detected-confirmed
    "delimiter": ",",
    "quotechar": '"',
    "header": True,
    "expected_columns": ["order_id", "cust_name", "amt", "dt", "active", "zip"],
}

# String-hygiene policies (STR / ENC / NUL-01).
POLICIES = {
    "unicode_normalization": "NFC",            # default
    "strip_control_chars": True,               # default
    "normalize_unicode_whitespace": True,      # default
    "trim_whitespace": True,                   # default
    "empty_string_is_null": True,              # explicit
}

# Run policies.
DATETIME_RENDERING = "iso8601"                 # default
ERROR_DISPOSITION = "quarantine"               # default
ERROR_BUDGET = {"percent": 25, "min_rows": 2}  # explicit
DUPLICATE_ROWS = "keep"                        # default (no de-dup applied)

# Target schema (name, nullable, max_length).
TARGET_COLUMNS = [
    ("order_id", False, None),
    ("customer_name", False, None),
    ("amount", True, None),
    ("order_date", True, None),
    ("is_active", True, None),
    ("postal_code", True, 10),
]
OUTPUT_HEADER = [c[0] for c in TARGET_COLUMNS]


def transform_row(raw):
    """Map one raw source dict -> (list of typed target values, fixes-by-column).

    Raises rt.RowError on any row-scoped failure; the caller quarantines.
    """
    fixes = {}  # {target_column: [tax_id, ...]}

    def clean(src_col):
        r = rt.apply_string_policies(raw.get(src_col), POLICIES)
        return r

    # order_id  <- order_id   (TYP-07: uniform 8-digit IDs kept as string)
    r = clean("order_id")
    fixes.setdefault("order_id", []).extend(r.fixes)
    order_id = rt.as_string(r.value).value
    order_id = rt.check_nullable(order_id, "order_id", nullable=False)

    # customer_name <- cust_name   (no transforms)
    r = clean("cust_name")
    fixes.setdefault("customer_name", []).extend(r.fixes)
    customer_name = rt.as_string(r.value).value
    customer_name = rt.check_nullable(customer_name, "customer_name",
                                      nullable=False)

    # amount <- amt   (TYP-01: strip $ and commas; (n) = negative)
    r = clean("amt")
    fixes.setdefault("amount", []).extend(r.fixes)
    dr = rt.to_decimal(r.value, scale=2, thousands_sep=",",
                       currency=True, accounting_negative=True)
    fixes["amount"].extend(dr.fixes)
    amount = rt.check_nullable(dr.value, "amount", nullable=True)

    # order_date <- dt   (TYP-03: MDY, explicit; sentinel 'N/A' -> NULL)
    r = clean("dt")
    fixes.setdefault("order_date", []).extend(r.fixes)
    dtr = rt.to_date(r.value, formats=["%m/%d/%Y"], sentinels=["N/A"])
    fixes["order_date"].extend(dtr.fixes)
    order_date = rt.check_nullable(dtr.value, "order_date", nullable=True)

    # is_active <- active   (TYP-06: Y/N vocabulary)
    r = clean("active")
    fixes.setdefault("is_active", []).extend(r.fixes)
    br = rt.to_bool(r.value, mapping={"Y": True, "N": False})
    fixes["is_active"].extend(br.fixes)
    is_active = rt.check_nullable(br.value, "is_active", nullable=True)

    # postal_code <- zip   (TYP-07: leading zeros -> string; max_length 10)
    r = clean("zip")
    fixes.setdefault("postal_code", []).extend(r.fixes)
    pc = rt.as_string(r.value, max_length=10)
    fixes["postal_code"].extend(pc.fixes)
    postal_code = rt.check_nullable(pc.value, "postal_code", nullable=True)

    values = [order_id, customer_name, amount, order_date, is_active,
              postal_code]
    return values, fixes


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    source_path = os.path.join(here, "eval-inputs", "vendor_orders_sample.csv")
    out_dir = os.path.join(here, "output")
    os.makedirs(out_dir, exist_ok=True)
    clean_path = os.path.join(out_dir, "vendor_orders_clean.csv")
    reject_path = os.path.join(out_dir, "vendor_orders_rejects.csv")
    manifest_path = os.path.join(out_dir, "vendor_orders_manifest.json")

    report = rt.RunReport(SPEC_NAME, TAXONOMY_VERSION, source_path)
    clean_rows = []

    n_expected = len(SOURCE["expected_columns"])

    with open(source_path, "r", encoding=SOURCE["encoding"], newline="") as f:
        reader = csv.reader(f, delimiter=SOURCE["delimiter"],
                            quotechar=SOURCE["quotechar"])
        header = next(reader, None)
        if header != SOURCE["expected_columns"]:
            # KEY-01: header shape mismatch is a fatal setup error, not a row error.
            sys.stderr.write(
                f"FATAL header mismatch: got {header!r}, "
                f"expected {SOURCE['expected_columns']!r}\n")
            return 1

        for line_no, raw_list in enumerate(reader, start=2):
            report.rows_read += 1
            raw_display = ",".join(raw_list)

            # STR-01 / ragged row: wrong field count -> quarantine, don't pad.
            if len(raw_list) != n_expected:
                err = rt.RowError(
                    "STR-01",
                    f"expected {n_expected} fields, got {len(raw_list)}")
                report.record_reject(line_no, raw_display, err)
                continue

            raw = dict(zip(SOURCE["expected_columns"], raw_list))
            try:
                values, fixes = transform_row(raw)
            except rt.RowError as err:
                report.record_reject(line_no, raw_display, err)
                continue

            for col, tax_ids in fixes.items():
                report.tally_fixes(col, tax_ids)

            rendered = [rt.render_cell(v, DATETIME_RENDERING) for v in values]
            clean_rows.append(rendered)  # DUPLICATE_ROWS=keep: never de-dup.
            report.rows_written += 1

    # Error-budget evaluation.
    exceeded, allowance = rt.evaluate_budget(
        report.rows_read, report.rows_quarantined, ERROR_BUDGET)

    if exceeded:
        exit_status = 1
    elif report.rows_quarantined > 0:
        exit_status = 2
    else:
        exit_status = 0

    # Atomic outputs (clean data, coded rejects, run manifest).
    rt.atomic_write_csv(clean_path, OUTPUT_HEADER, clean_rows)

    reject_header = ["row_number", "tax_id", "column", "reason", "raw"]
    reject_rows = [
        [r["row_number"], r["tax_id"], r["column"] or "", r["reason"], r["raw"]]
        for r in report.reject_records
    ]
    rt.atomic_write_csv(reject_path, reject_header, reject_rows)

    manifest = report.as_manifest(
        source_sha256=rt.sha256_file(source_path),
        error_budget=ERROR_BUDGET,
        budget_exceeded=exceeded,
        exit_status=exit_status,
        output_path=clean_path,
        reject_path=reject_path,
    )
    rt.atomic_write_json(manifest_path, manifest)

    # Human-facing run summary (ERR-03 coarse granularity).
    print(f"[{SPEC_NAME}] rows_read={report.rows_read} "
          f"written={report.rows_written} "
          f"quarantined={report.rows_quarantined} "
          f"auto_fixes={report.total_fixes()}")
    print(f"  error budget: quarantined {report.rows_quarantined} "
          f"of allowance {allowance:.2f} -> "
          f"{'EXCEEDED' if exceeded else 'within budget'}")
    if report.fixes:
        print("  auto-fixes by column:")
        for col, tallies in report.fixes.items():
            if tallies:
                pretty = ", ".join(f"{k}:{v}" for k, v in tallies.items())
                print(f"    {col}: {pretty}")
    if report.error_types:
        print("  quarantine reasons:")
        for tax_id, count in report.error_types.items():
            print(f"    {tax_id}: {count}")
    print(f"  outputs: {clean_path}")
    print(f"           {reject_path}")
    print(f"           {manifest_path}")
    print(f"  exit_status={exit_status}")
    return exit_status


if __name__ == "__main__":
    sys.exit(main())
