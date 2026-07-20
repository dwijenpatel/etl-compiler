#!/usr/bin/env python3
"""Pipeline: vendor_orders

GENERATED from /home/claude/work/eval-inputs/vendor_orders.etlspec.yaml
(etlspec 0.1, taxonomy_version 0.1). Do not hand-edit decisions here; change
the spec and regenerate.

Spec decisions encoded in this module (unchanged from the spec):
  - Source: CSV, utf-8, delimiter ",", quotechar '"', header present,
    expected columns [order_id, cust_name, amt, dt, active, zip].
  - Policies: NFC unicode normalization; strip control chars; normalize
    unicode whitespace; trim whitespace; empty string -> NULL; SQL null
    propagation; ISO-8601 datetime rendering; error disposition = quarantine;
    error budget = 25% with min_rows 2; duplicate rows kept.
  - Mappings:
      order_id       <- order_id  (string, not null)          [TYP-07: string]
      customer_name  <- cust_name (string, not null)
      amount         <- amt       (decimal scale 2, nullable) [TYP-01: strip $
                                   and commas; (n) = negative]
      order_date     <- dt        (date, nullable, %m/%d/%Y)  [TYP-03: MDY],
                                   sentinel "N/A" -> NULL
      is_active      <- active    (boolean, nullable)         [TYP-06: Y/N]
      postal_code    <- zip       (string, nullable, max_length 10)
                                                              [TYP-07: string]

Usage:
    python3 vendor_orders_pipeline.py INPUT_CSV OUTPUT_CSV \
        [--quarantine QUARANTINE_CSV] [--report REPORT_JSON]

Exit codes:
    0 - success (quarantined rows, if any, are within the error budget)
    1 - error budget exceeded, or fatal input error (missing/extra columns,
        unreadable file)
"""

import argparse
import csv
import json
import math
import re
import sys
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# --------------------------------------------------------------------------
# Spec constants
# --------------------------------------------------------------------------

SPEC_NAME = "vendor_orders"

SOURCE_ENCODING = "utf-8"
SOURCE_DELIMITER = ","
SOURCE_QUOTECHAR = '"'
EXPECTED_COLUMNS = ["order_id", "cust_name", "amt", "dt", "active", "zip"]

TARGET_COLUMNS = [
    "order_id",
    "customer_name",
    "amount",
    "order_date",
    "is_active",
    "postal_code",
]

# policies.error_budget: {percent: 25, min_rows: 2}
ERROR_BUDGET_PERCENT = 25
ERROR_BUDGET_MIN_ROWS = 2

# mappings.order_date.transforms.to_date.formats
DATE_FORMATS = ["%m/%d/%Y"]
# mappings.order_date.sentinels.values
DATE_SENTINELS = {"N/A"}

# mappings.is_active.transforms.to_bool.mapping (TYP-06: Y/N vocabulary)
BOOL_MAPPING = {"Y": True, "N": False}

# target.columns.postal_code.max_length
POSTAL_CODE_MAX_LENGTH = 10

# amount: decimal scale 2
AMOUNT_QUANT = Decimal("0.01")

# --------------------------------------------------------------------------
# Cell cleaning (policies)
# --------------------------------------------------------------------------

# Unicode whitespace characters normalized to ASCII space
# (policies.normalize_unicode_whitespace = true)
_UNICODE_WS_RE = re.compile(
    "[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000\u180e\u200b\ufeff]"
)


def clean_cell(raw):
    """Apply spec text policies to a raw CSV cell; returns str or None.

    Order: NFC normalization -> strip control chars -> normalize unicode
    whitespace -> trim -> empty string is NULL.
    """
    if raw is None:
        return None
    # policies.unicode_normalization = NFC
    value = unicodedata.normalize("NFC", raw)
    # policies.strip_control_chars = true (keep tab/newline handling simple:
    # control chars are removed; tabs/newlines inside cells count as control)
    value = "".join(
        ch for ch in value if unicodedata.category(ch) != "Cc"
    )
    # policies.normalize_unicode_whitespace = true
    value = _UNICODE_WS_RE.sub(" ", value)
    # policies.trim_whitespace = true
    value = value.strip()
    # policies.empty_string_is_null = true
    if value == "":
        return None
    return value


# --------------------------------------------------------------------------
# Transforms
# --------------------------------------------------------------------------

class TransformError(ValueError):
    """A cell value that cannot be converted per the spec."""


def to_decimal(value, thousands_sep=",", currency=True, accounting_negative=True):
    """TYP-01: strip $ and commas; (n) = negative. Scale 2, HALF_UP."""
    if value is None:
        return None
    text = value
    negative = False
    if accounting_negative and text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()
    if currency:
        text = text.replace("$", "").strip()
    if thousands_sep:
        text = text.replace(thousands_sep, "")
    if text.startswith("-"):
        negative = True
        text = text[1:]
    if text == "":
        raise TransformError("empty numeric value after stripping")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise TransformError("not a valid decimal: %r" % value)
    if negative:
        number = -number
    return number.quantize(AMOUNT_QUANT, rounding=ROUND_HALF_UP)


def to_date(value, formats=DATE_FORMATS, sentinels=DATE_SENTINELS):
    """TYP-03: MDY (%m/%d/%Y). Sentinel 'N/A' -> NULL."""
    if value is None:
        return None
    if value in sentinels:
        return None
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise TransformError("not a valid date (expected MDY %r): %r" % (formats, value))


def to_bool(value, mapping=BOOL_MAPPING):
    """TYP-06: Y/N vocabulary."""
    if value is None:
        return None
    if value in mapping:
        return mapping[value]
    raise TransformError("not in boolean vocabulary %r: %r" % (sorted(mapping), value))


# --------------------------------------------------------------------------
# Rendering (policies.datetime_rendering = iso8601)
# --------------------------------------------------------------------------

def render(value):
    if value is None:
        return ""  # NULL rendered as empty field
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


# --------------------------------------------------------------------------
# Row transformation
# --------------------------------------------------------------------------

def transform_row(row):
    """Map a cleaned source row dict to a target row dict.

    Raises TransformError with a field-scoped message on failure.
    """
    errors = []
    out = {}

    # order_id <- order_id (string, not null)
    order_id = clean_cell(row.get("order_id"))
    if order_id is None:
        errors.append("order_id: null value in non-nullable column")
    out["order_id"] = order_id

    # customer_name <- cust_name (string, not null)
    customer_name = clean_cell(row.get("cust_name"))
    if customer_name is None:
        errors.append("customer_name: null value in non-nullable column")
    out["customer_name"] = customer_name

    # amount <- amt (decimal scale 2, nullable)
    try:
        out["amount"] = to_decimal(clean_cell(row.get("amt")))
    except TransformError as exc:
        errors.append("amount: %s" % exc)

    # order_date <- dt (date, nullable; sentinel N/A -> NULL)
    try:
        out["order_date"] = to_date(clean_cell(row.get("dt")))
    except TransformError as exc:
        errors.append("order_date: %s" % exc)

    # is_active <- active (boolean, nullable)
    try:
        out["is_active"] = to_bool(clean_cell(row.get("active")))
    except TransformError as exc:
        errors.append("is_active: %s" % exc)

    # postal_code <- zip (string, nullable, max_length 10)
    postal_code = clean_cell(row.get("zip"))
    if postal_code is not None and len(postal_code) > POSTAL_CODE_MAX_LENGTH:
        errors.append(
            "postal_code: length %d exceeds max_length %d"
            % (len(postal_code), POSTAL_CODE_MAX_LENGTH)
        )
    out["postal_code"] = postal_code

    if errors:
        raise TransformError("; ".join(errors))
    return out


# --------------------------------------------------------------------------
# Pipeline driver
# --------------------------------------------------------------------------

def run(input_path, output_path, quarantine_path, report_path):
    report = {
        "pipeline": SPEC_NAME,
        "spec": "vendor_orders.etlspec.yaml (etlspec 0.1)",
        "input": input_path,
        "output": output_path,
        "quarantine": quarantine_path,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "rows_read": 0,
        "rows_written": 0,
        "rows_quarantined": 0,
        "errors": [],
        "status": "unknown",
    }

    try:
        infile = open(input_path, "r", encoding=SOURCE_ENCODING, newline="")
    except OSError as exc:
        report["status"] = "failed"
        report["errors"].append("cannot open input: %s" % exc)
        _write_report(report_path, report)
        print("FATAL: %s" % exc, file=sys.stderr)
        return 1

    with infile, \
            open(output_path, "w", encoding="utf-8", newline="") as outfile, \
            open(quarantine_path, "w", encoding="utf-8", newline="") as qfile:

        reader = csv.DictReader(
            infile, delimiter=SOURCE_DELIMITER, quotechar=SOURCE_QUOTECHAR
        )

        # Header validation against spec expected_columns
        header = reader.fieldnames or []
        if header != EXPECTED_COLUMNS:
            report["status"] = "failed"
            msg = "header mismatch: expected %r, got %r" % (EXPECTED_COLUMNS, header)
            report["errors"].append(msg)
            _write_report(report_path, report)
            print("FATAL: %s" % msg, file=sys.stderr)
            return 1

        writer = csv.DictWriter(
            outfile, fieldnames=TARGET_COLUMNS, quoting=csv.QUOTE_MINIMAL
        )
        writer.writeheader()

        qwriter = csv.DictWriter(
            qfile,
            fieldnames=["_row_number", "_error"] + EXPECTED_COLUMNS,
            quoting=csv.QUOTE_MINIMAL,
        )
        qwriter.writeheader()

        # policies.duplicate_rows = keep -> no dedup pass
        for row_number, row in enumerate(reader, start=2):  # line 1 = header
            report["rows_read"] += 1
            try:
                out_row = transform_row(row)
            except TransformError as exc:
                # policies.error_disposition = quarantine
                report["rows_quarantined"] += 1
                report["errors"].append(
                    {"row": row_number, "error": str(exc)}
                )
                qrow = {"_row_number": row_number, "_error": str(exc)}
                qrow.update({k: row.get(k) for k in EXPECTED_COLUMNS})
                qwriter.writerow(qrow)
                continue
            writer.writerow({k: render(v) for k, v in out_row.items()})
            report["rows_written"] += 1

    # policies.error_budget = {percent: 25, min_rows: 2}
    # Allowed quarantined rows = max(floor(25% of rows read), 2).
    budget = max(
        math.floor(report["rows_read"] * ERROR_BUDGET_PERCENT / 100.0),
        ERROR_BUDGET_MIN_ROWS,
    )
    report["error_budget_allowed"] = budget

    if report["rows_quarantined"] > budget:
        report["status"] = "failed"
        exit_code = 1
        print(
            "FAILED: %d row(s) quarantined exceeds error budget of %d"
            % (report["rows_quarantined"], budget),
            file=sys.stderr,
        )
    else:
        report["status"] = "success"
        exit_code = 0

    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    _write_report(report_path, report)

    print(
        "%s: read=%d written=%d quarantined=%d (budget=%d) -> %s"
        % (
            report["status"].upper(),
            report["rows_read"],
            report["rows_written"],
            report["rows_quarantined"],
            budget,
            output_path,
        )
    )
    return exit_code


def _write_report(report_path, report):
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
            fh.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the %s pipeline." % SPEC_NAME)
    parser.add_argument("input_csv")
    parser.add_argument("output_csv")
    parser.add_argument("--quarantine", default=None)
    parser.add_argument("--report", default=None)
    args = parser.parse_args(argv)

    quarantine = args.quarantine or (args.output_csv + ".quarantine.csv")
    report = args.report or (args.output_csv + ".report.json")
    return run(args.input_csv, args.output_csv, quarantine, report)


if __name__ == "__main__":
    sys.exit(main())
