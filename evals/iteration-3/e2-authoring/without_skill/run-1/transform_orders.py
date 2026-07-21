#!/usr/bin/env python3
"""
transform_orders.py

Transforms a vendor order export (orders_export.csv) into the warehouse
order format demonstrated by target_orders.csv.

Written from scratch (stdlib only). Run:

    python3 transform_orders.py \
        --input  eval-inputs/orders_export.csv \
        --output orders_transformed.csv

Design notes / decisions are documented in REVIEW.md. Every transformation,
drop, pad, and repair is counted and reported in run_summary.json so nothing
happens silently.

Target format (from target_orders.csv):
    order_id,customer_name,amount,order_date,is_active,postal_code,notes
    10000001,José,1234.56,2026-01-02,true,02134,ok

Source header (orders_export.csv), note the stray spaces:
    order_id, customer ,amt,order_date,active,zip,notes
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections import Counter
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

# --------------------------------------------------------------------------
# Configuration derived from the export + target sample (see REVIEW.md)
# --------------------------------------------------------------------------

TARGET_COLUMNS = [
    "order_id",
    "customer_name",
    "amount",
    "order_date",
    "is_active",
    "postal_code",
    "notes",
]

# Source column name (whitespace-stripped) -> target column name.
COLUMN_MAP = {
    "order_id": "order_id",
    "customer": "customer_name",
    "amt": "amount",
    "order_date": "order_date",
    "active": "is_active",
    "zip": "postal_code",
    "notes": "notes",
}

# Source date format. MM/DD/YYYY (month-first) is confirmed by the target
# sample: 01/02/2026 -> 2026-01-02 (month=01, day=02). See TYP-03 in REVIEW.md.
SOURCE_DATE_FMT = "%m/%d/%Y"

# Case-insensitive tokens that mean "no value" and are normalized to empty.
NULL_SENTINELS = {"", "N/A", "NA", "NULL", "NONE", "-", "--"}

# Truthy / falsy vocabulary for the active flag.
TRUE_TOKENS = {"Y", "YES", "TRUE", "T", "1"}
FALSE_TOKENS = {"N", "NO", "FALSE", "F", "0"}

# Characters that strongly indicate UTF-8-decoded-as-Latin1 mojibake.
MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "Ð", "Ñ")

# Unicode categories for control (Cc) and format (Cf) chars we scrub from text.
CONTROL_CATEGORIES = {"Cc", "Cf"}


def strip_control_chars(value: str) -> tuple[str, int]:
    """Remove control/format chars (e.g. U+000B vertical tab hidden in text).

    Returns (cleaned, count_removed). Interior removal can be meaning-changing
    (e.g. 'Bob\\x0bSmith' -> 'BobSmith' vs 'Bob Smith'), so callers flag it.
    """
    out, removed = [], 0
    for ch in value:
        if unicodedata.category(ch) in CONTROL_CATEGORIES:
            removed += 1
        else:
            out.append(ch)
    return "".join(out), removed


# --------------------------------------------------------------------------
# Field-level transforms
# --------------------------------------------------------------------------

def repair_mojibake(value: str) -> tuple[str, bool]:
    """Repair classic double-encoding mojibake (UTF-8 bytes read as Latin-1).

    e.g. 'JosÃ© GarcÃ­a' -> 'José García'. Only applied when marker chars are
    present AND the latin-1 -> utf-8 round-trip succeeds and removes the
    markers; otherwise the original string is returned untouched so that
    already-correct accented names (José, Réné) are never damaged.
    Returns (value, repaired?).
    """
    if not any(m in value for m in MOJIBAKE_MARKERS):
        return value, False
    try:
        candidate = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value, False
    if any(m in candidate for m in ("Ã", "Â")):
        # Round-trip did not actually clean it up; leave original.
        return value, False
    return candidate, True


def clean_customer(raw: str, stats: Counter, flags: list[str]) -> str:
    # Order matters: repair mojibake BEFORE scrubbing control/format chars.
    # A double-encoded byte sequence (e.g. 'í' -> ...c2 ad...) can surface a
    # soft hyphen (U+00AD, category Cf) mid-sequence; scrubbing first would
    # corrupt the repair.
    s = raw.strip()
    s, repaired = repair_mojibake(s)
    if repaired:
        stats["mojibake_repaired"] += 1
        flags.append("repaired mojibake in customer name")
    s, removed = strip_control_chars(s)
    if removed:
        stats["control_chars_stripped"] += removed
        flags.append(
            f"removed {removed} control char(s) from customer "
            f"(e.g. hidden separator); confirm intended spelling"
        )
    s = unicodedata.normalize("NFC", s)
    return s


def parse_amount(raw: str) -> Decimal | None:
    """Parse currency/accounting text into a Decimal with scale 2.

    Handles: '$1,234.56' -> 1234.56 ; '(500)' -> -500.00 ; '250.00' -> 250.00.
    Rules (see TYP-01 in REVIEW.md): '$' and thousands ',' stripped;
    parentheses OR a leading '-' denote a negative. '.' is the decimal point.
    """
    s = raw.strip()
    if s in NULL_SENTINELS:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    if s.startswith("-"):
        negative = not negative
        s = s[1:].strip()
    # Strip everything that is not a digit or a decimal point ($ , whitespace).
    cleaned = re.sub(r"[^0-9.]", "", s)
    if cleaned in ("", "."):
        raise ValueError(f"unparseable amount: {raw!r}")
    try:
        d = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"unparseable amount: {raw!r}")
    if negative:
        d = -d
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def parse_date(raw: str) -> str | None:
    """MM/DD/YYYY -> ISO YYYY-MM-DD. Sentinels -> None."""
    s = raw.strip()
    if s.upper() in NULL_SENTINELS:
        return None
    dt = datetime.strptime(s, SOURCE_DATE_FMT)
    return dt.strftime("%Y-%m-%d")


def parse_bool(raw: str) -> str | None:
    """Y/N (and friends) -> 'true'/'false'. Sentinels -> None."""
    s = raw.strip().upper()
    if s in NULL_SENTINELS:
        return None
    if s in TRUE_TOKENS:
        return "true"
    if s in FALSE_TOKENS:
        return "false"
    raise ValueError(f"unrecognized boolean: {raw!r}")


def clean_postal(raw: str) -> str:
    """Keep as a string; preserve leading zeros. Just trim."""
    return raw.strip()


def clean_notes(raw: str, stats: Counter, flags: list[str]) -> str:
    s, removed = strip_control_chars(raw)
    if removed:
        stats["control_chars_stripped"] += removed
        flags.append(f"removed {removed} control char(s) from notes")
    s = unicodedata.normalize("NFC", s.strip())
    if s.upper() in NULL_SENTINELS:
        if raw.strip() != "":
            stats["notes_sentinel_to_empty"] += 1
        return ""
    return s


# --------------------------------------------------------------------------
# Row-level driver
# --------------------------------------------------------------------------

def looks_like_data_row(order_id: str) -> bool:
    """Data rows have an all-digit order_id. 'Total' / blanks do not."""
    return order_id.strip().isdigit()


def atomic_write_csv(path: str, header: list[str], rows: list[list[str]]) -> None:
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            # LF line endings to match the target sample (target uses \n, not \r\n).
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(header)
            w.writerows(rows)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def transform(input_path: str, output_path: str, reject_path: str,
              summary_path: str) -> dict:
    stats: Counter = Counter()
    review: list[dict] = []  # notes for the human
    out_rows: list[list[str]] = []
    rejected: list[list[str]] = []  # [line_no, reason, raw...]
    seen_full: dict[tuple, int] = {}   # full transformed tuple -> first line
    seen_order_id: dict[str, int] = {}

    # utf-8-sig transparently strips the BOM; universal newlines handle CRLF/LF.
    with open(input_path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        try:
            raw_header = next(reader)
        except StopIteration:
            raise SystemExit("input file is empty")

        header = [h.strip() for h in raw_header]
        # Build source-name -> index using stripped names.
        idx = {name: i for i, name in enumerate(header)}
        missing = [src for src in COLUMN_MAP if src not in idx]
        if missing:
            raise SystemExit(
                f"source is missing expected column(s): {missing}; "
                f"found header: {header}"
            )
        n_expected = len(header)

        for line_no, row in enumerate(reader, start=2):
            stats["rows_read"] += 1

            # Skip completely blank lines.
            if not row or all(c.strip() == "" for c in row):
                stats["blank_rows_skipped"] += 1
                continue

            # Ragged handling: pad short rows, reject over-long rows.
            if len(row) < n_expected:
                padded = row + [""] * (n_expected - len(row))
                stats["ragged_rows_padded"] += 1
                review.append({
                    "line": line_no,
                    "kind": "ragged_padded",
                    "detail": f"had {len(row)} of {n_expected} fields; "
                              f"missing trailing column(s) padded empty",
                    "raw": row,
                })
                row = padded
            elif len(row) > n_expected:
                stats["rows_rejected"] += 1
                rejected.append([str(line_no), "too_many_fields"] + row)
                review.append({
                    "line": line_no,
                    "kind": "rejected_too_many_fields",
                    "detail": f"had {len(row)} fields; expected {n_expected}",
                    "raw": row,
                })
                continue

            order_id = row[idx["order_id"]].strip()

            # Footer / summary rows (e.g. 'Total,,3222.56,...') are not orders.
            if not looks_like_data_row(order_id):
                stats["footer_rows_dropped"] += 1
                rejected.append([str(line_no), "non_data_row"] + row)
                review.append({
                    "line": line_no,
                    "kind": "non_data_row_dropped",
                    "detail": f"order_id={order_id!r} is not numeric; "
                              f"treated as footer/summary and excluded",
                    "raw": row,
                })
                continue

            # Field transforms; any hard failure quarantines the row.
            row_flags: list[str] = []
            try:
                customer = clean_customer(row[idx["customer"]], stats, row_flags)
                amount = parse_amount(row[idx["amt"]])
                order_date = parse_date(row[idx["order_date"]])
                is_active = parse_bool(row[idx["active"]])
                postal = clean_postal(row[idx["zip"]])
                notes = clean_notes(row[idx["notes"]], stats, row_flags)
            except ValueError as e:
                stats["rows_rejected"] += 1
                rejected.append([str(line_no), f"transform_error: {e}"] + row)
                review.append({
                    "line": line_no,
                    "kind": "rejected_transform_error",
                    "detail": str(e),
                    "raw": row,
                })
                continue

            out = [
                order_id,
                customer,
                "" if amount is None else str(amount),
                order_date or "",
                is_active or "",
                postal,
                notes,
            ]

            # Duplicate detection (kept, not dropped — see REVIEW.md).
            sig = tuple(out)
            if sig in seen_full:
                stats["exact_duplicate_rows"] += 1
                review.append({
                    "line": line_no,
                    "kind": "exact_duplicate_kept",
                    "detail": f"row is byte-identical to line {seen_full[sig]} "
                              f"(order_id={order_id}); KEPT in output, flag for "
                              f"review",
                    "raw": row,
                })
            else:
                seen_full[sig] = line_no

            if order_id in seen_order_id and sig not in seen_full:
                stats["duplicate_order_id"] += 1
            seen_order_id.setdefault(order_id, line_no)

            if row_flags:
                review.append({
                    "line": line_no,
                    "kind": "field_cleaned",
                    "detail": "; ".join(row_flags),
                    "raw": row,
                })

            out_rows.append(out)
            stats["rows_written"] += 1

    atomic_write_csv(output_path, TARGET_COLUMNS, out_rows)
    if rejected:
        reject_header = ["source_line", "reason"] + header
        atomic_write_csv(reject_path, reject_header, rejected)

    summary = {
        "input": os.path.abspath(input_path),
        "output": os.path.abspath(output_path),
        "rejects_file": os.path.abspath(reject_path) if rejected else None,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counts": dict(sorted(stats.items())),
        "review_items": review,
    }
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)

    return summary


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default=os.path.join(here, "eval-inputs", "orders_export.csv"))
    ap.add_argument("--output", default=os.path.join(here, "orders_transformed.csv"))
    ap.add_argument("--rejects", default=os.path.join(here, "orders_rejected.csv"))
    ap.add_argument("--summary", default=os.path.join(here, "run_summary.json"))
    args = ap.parse_args()

    summary = transform(args.input, args.output, args.rejects, args.summary)
    c = summary["counts"]

    print("ETL run complete.")
    print(f"  read     : {c.get('rows_read', 0)}")
    print(f"  written  : {c.get('rows_written', 0)}")
    print(f"  rejected : {c.get('rows_rejected', 0)}")
    print(f"  footer dropped        : {c.get('footer_rows_dropped', 0)}")
    print(f"  blank skipped         : {c.get('blank_rows_skipped', 0)}")
    print(f"  ragged padded         : {c.get('ragged_rows_padded', 0)}")
    print(f"  exact duplicates kept : {c.get('exact_duplicate_rows', 0)}")
    print(f"  mojibake repaired     : {c.get('mojibake_repaired', 0)}")
    print(f"  control chars stripped: {c.get('control_chars_stripped', 0)}")
    print(f"  notes sentinel->empty : {c.get('notes_sentinel_to_empty', 0)}")
    print(f"  output  -> {summary['output']}")
    if summary["rejects_file"]:
        print(f"  rejects -> {summary['rejects_file']}")
    print(f"  summary -> {os.path.abspath(args.summary)}")

    # Exit 2 signals "completed but rows need review", 0 = fully clean.
    needs_review = (
        c.get("rows_rejected", 0)
        or c.get("footer_rows_dropped", 0)
        or c.get("ragged_rows_padded", 0)
        or c.get("exact_duplicate_rows", 0)
    )
    return 2 if needs_review else 0


if __name__ == "__main__":
    sys.exit(main())
