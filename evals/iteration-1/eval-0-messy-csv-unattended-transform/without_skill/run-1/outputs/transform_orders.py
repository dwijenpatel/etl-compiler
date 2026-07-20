#!/usr/bin/env python3
"""
ETL: vendor orders export -> warehouse orders format.

Source (vendor export), e.g. /home/claude/work/eval-inputs/orders_export.csv:
    order_id, customer ,amt,order_date,active,zip,notes

Target (warehouse), matching /home/claude/work/eval-inputs/target_orders.csv:
    order_id,customer_name,amount,order_date,is_active,postal_code,notes

Transformations applied
-----------------------
1.  Header normalization + rename:
        order_id -> order_id
        customer -> customer_name   (vendor header has stray spaces: " customer ")
        amt      -> amount
        order_date -> order_date
        active   -> is_active
        zip      -> postal_code
        notes    -> notes
2.  amount: strip currency symbols and thousands separators ("$1,234.56"),
    treat accounting parentheses "(500)" as negative, output with exactly
    2 decimal places (Decimal arithmetic, no float rounding surprises).
3.  order_date: MM/DD/YYYY -> ISO YYYY-MM-DD.  (Confirmed against the target
    sample: 01/02/2026 -> 2026-01-02, i.e. US month-first.)
4.  is_active: Y/N (and common variants) -> true/false.
5.  customer_name: repair UTF-8 mojibake (double-encoded text such as
    "JosÃ© GarcÃ­a" -> "José García"), collapse/strip whitespace.
6.  postal_code: kept as text so leading zeros survive (02134 stays 02134).
7.  notes: "N/A"-style placeholders and whitespace-only values -> empty;
    rows short one field (vendor sometimes omits trailing notes) -> empty.
8.  Row filtering: blank lines, summary/footer rows (e.g. "Total,...."), and
    exact duplicate data rows are dropped.  Every dropped row is logged to the
    run report with a reason.

Usage:
    python3 transform_orders.py INPUT_CSV OUTPUT_CSV [--report REPORT_JSON]

Exit codes: 0 = success, 1 = fatal error (bad input / no valid rows),
            2 = completed but with rows quarantined by validation errors.
"""

import argparse
import csv
import io
import json
import re
import sys
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

# ---------------------------------------------------------------------------
# Column mapping: normalized vendor header -> warehouse column
# ---------------------------------------------------------------------------
COLUMN_MAP = {
    "order_id": "order_id",
    "customer": "customer_name",
    "amt": "amount",
    "order_date": "order_date",
    "active": "is_active",
    "zip": "postal_code",
    "notes": "notes",
}
OUTPUT_COLUMNS = [
    "order_id", "customer_name", "amount", "order_date",
    "is_active", "postal_code", "notes",
]

NA_PLACEHOLDERS = {"n/a", "na", "none", "null", "-", "--"}
TRUE_VALUES = {"y", "yes", "true", "t", "1"}
FALSE_VALUES = {"n", "no", "false", "f", "0"}
# Vendor rows whose first cell is one of these are footer/summary rows.
SUMMARY_MARKERS = {"total", "subtotal", "grand total", "sum"}

MOJIBAKE_HINT = re.compile(r"[ÃÂ][\x80-\xBF-ÿ]")


def fix_mojibake(text: str) -> str:
    """Repair UTF-8 text that was decoded as Latin-1/cp1252 and re-encoded
    (classic double-encoding: 'José' -> 'JosÃ©').  Applied repeatedly in case
    of multiple rounds; falls back to the original string if repair fails."""
    result = text
    for _ in range(3):  # handle up to triple-encoding
        if not MOJIBAKE_HINT.search(result):
            break
        for codec in ("latin-1", "cp1252"):
            try:
                candidate = result.encode(codec).decode("utf-8")
                break
            except (UnicodeEncodeError, UnicodeDecodeError):
                candidate = None
        if candidate is None or candidate == result:
            break
        result = candidate
    return result


def clean_text(value):
    """Trim, collapse internal whitespace (incl. NBSP/vertical tab artifacts),
    and normalize to NFC so accented chars match the warehouse's precomposed
    form.  None -> empty string."""
    if value is None:
        return ""
    collapsed = re.sub(r"\s+", " ", value).strip()
    return unicodedata.normalize("NFC", collapsed)


def parse_amount(raw: str) -> Decimal:
    """Parse '$1,234.56', '(500)', '250.00', '1000' etc. into a Decimal."""
    s = clean_text(raw)
    if not s:
        raise ValueError("empty amount")
    negative = False
    if s.startswith("(") and s.endswith(")"):  # accounting negative
        negative = True
        s = s[1:-1]
    s = s.replace("$", "").replace("€", "").replace("£", "")
    s = s.replace(",", "").replace(" ", "")
    if s.startswith("-"):
        negative = not s.startswith("--") and True
        s = s.lstrip("-")
    try:
        value = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"unparseable amount: {raw!r}")
    return -value if negative else value


def parse_date(raw: str) -> str:
    """Vendor uses US-style MM/DD/YYYY (verified against the target sample:
    01/02/2026 -> 2026-01-02).  Also accepts already-ISO dates."""
    s = clean_text(raw)
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"unparseable date: {raw!r}")


def parse_bool(raw: str) -> str:
    s = clean_text(raw).lower()
    if s in TRUE_VALUES:
        return "true"
    if s in FALSE_VALUES:
        return "false"
    raise ValueError(f"unparseable boolean: {raw!r}")


def clean_notes(raw) -> str:
    s = clean_text(raw)
    if s.lower() in NA_PLACEHOLDERS:
        return ""
    return s


def clean_postal_code(raw: str) -> str:
    """Keep as text so leading zeros survive.  Zero-pad plain-numeric US zips
    that lost their leading zero upstream (e.g. '2134' -> '02134')."""
    s = clean_text(raw)
    if s.isdigit() and len(s) < 5:
        s = s.zfill(5)
    return s


def transform_row(row: dict) -> dict:
    """Map one normalized vendor row -> warehouse row.  Raises ValueError on
    any field that fails validation."""
    out = {}
    order_id = clean_text(row.get("order_id"))
    if not order_id.isdigit():
        raise ValueError(f"non-numeric order_id: {order_id!r}")
    out["order_id"] = order_id
    out["customer_name"] = fix_mojibake(clean_text(row.get("customer")))
    if not out["customer_name"]:
        raise ValueError("missing customer name")
    amount = parse_amount(row.get("amt", ""))
    out["amount"] = f"{amount:.2f}"
    out["order_date"] = parse_date(row.get("order_date", ""))
    out["is_active"] = parse_bool(row.get("active", ""))
    out["postal_code"] = clean_postal_code(row.get("zip", ""))
    out["notes"] = clean_notes(row.get("notes"))
    return out


def run(input_path: Path, output_path: Path, report_path: Path) -> int:
    report = {
        "input_file": str(input_path),
        "output_file": str(output_path),
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "rows_read": 0,
        "rows_written": 0,
        "dropped_rows": [],   # {line, reason, raw}
        "quarantined_rows": [],  # rows that failed validation
        "warnings": [],
    }

    raw_bytes = input_path.read_bytes()
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw_bytes.decode("cp1252")
        report["warnings"].append("input was not valid UTF-8; decoded as cp1252")

    # NOTE: do not use text.splitlines() here -- it splits on \v/\f control
    # chars that this vendor embeds inside fields.  StringIO(newline="")
    # lets the csv module split records on real newlines only, and also
    # handles newlines inside quoted fields correctly.
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        print("FATAL: input file is empty", file=sys.stderr)
        return 1

    normalized = [h.strip().lower() for h in header]
    unknown = [h for h in normalized if h and h not in COLUMN_MAP]
    if unknown:
        report["warnings"].append(f"unrecognized source columns ignored: {unknown}")
    missing = [c for c in COLUMN_MAP if c not in normalized]
    if missing:
        print(f"FATAL: input is missing expected columns: {missing}", file=sys.stderr)
        return 1

    out_rows = []
    seen_exact = {}   # full output-row tuple -> first source line
    seen_ids = {}     # order_id -> (line, row) for conflict detection

    for line_no, fields in enumerate(reader, start=2):
        report["rows_read"] += 1
        raw_joined = ",".join(fields)

        # blank / whitespace-only lines
        if not fields or all(not f.strip() for f in fields):
            report["dropped_rows"].append(
                {"line": line_no, "reason": "blank line", "raw": raw_joined})
            continue

        # footer / summary rows ("Total,,3222.56,...")
        if fields[0].strip().lower() in SUMMARY_MARKERS:
            report["dropped_rows"].append(
                {"line": line_no, "reason": "summary/footer row", "raw": raw_joined})
            continue

        # tolerate rows short of trailing fields (vendor drops empty notes)
        if len(fields) < len(normalized):
            fields = fields + [""] * (len(normalized) - len(fields))
        elif len(fields) > len(normalized):
            report["quarantined_rows"].append(
                {"line": line_no, "reason": "too many fields", "raw": raw_joined})
            continue

        row = {normalized[i]: fields[i] for i in range(len(normalized))}
        try:
            out = transform_row(row)
        except ValueError as exc:
            report["quarantined_rows"].append(
                {"line": line_no, "reason": str(exc), "raw": raw_joined})
            continue

        key = tuple(out[c] for c in OUTPUT_COLUMNS)
        if key in seen_exact:
            report["dropped_rows"].append(
                {"line": line_no,
                 "reason": f"exact duplicate of line {seen_exact[key]}",
                 "raw": raw_joined})
            continue
        seen_exact[key] = line_no

        if out["order_id"] in seen_ids:
            # same id but different data: keep both, flag for human review
            report["warnings"].append(
                f"order_id {out['order_id']} appears on lines "
                f"{seen_ids[out['order_id']]} and {line_no} with different data "
                "- both kept, review needed")
        else:
            seen_ids[out["order_id"]] = line_no

        out_rows.append(out)

    if not out_rows:
        print("FATAL: no valid rows produced", file=sys.stderr)
        return 1

    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        # Unix newlines to match the warehouse sample (csv default is \r\n).
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(out_rows)
    report["rows_written"] = len(out_rows)

    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")

    print(f"rows read:        {report['rows_read']}")
    print(f"rows written:     {report['rows_written']}")
    print(f"rows dropped:     {len(report['dropped_rows'])} "
          f"(blank/summary/duplicate - expected, see report)")
    print(f"rows quarantined: {len(report['quarantined_rows'])} (validation failures)")
    for w in report["warnings"]:
        print(f"WARNING: {w}")
    print(f"output:  {output_path}")
    print(f"report:  {report_path}")
    return 2 if report["quarantined_rows"] else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("input_csv", type=Path)
    ap.add_argument("output_csv", type=Path)
    ap.add_argument("--report", type=Path, default=None,
                    help="path for the JSON run report "
                         "(default: <output>.report.json)")
    args = ap.parse_args()
    report = args.report or args.output_csv.with_suffix(".report.json")
    sys.exit(run(args.input_csv, args.output_csv, report))


if __name__ == "__main__":
    main()
