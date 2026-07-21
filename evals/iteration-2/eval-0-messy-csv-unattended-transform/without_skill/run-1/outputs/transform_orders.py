#!/usr/bin/env python3
"""
transform_orders.py

Transform a vendor order export (orders_export.csv) into our warehouse
`target_orders` format.

Usage:
    python3 transform_orders.py \
        --input  eval-inputs/orders_export.csv \
        --output orders_transformed.csv

Stdlib only. Every transformation is counted; every dropped/quarantined row
is recorded with a reason so the run is fully accountable. A machine-readable
run summary is written to run_summary.json and any rejected rows to
quarantine.csv.

Target schema (column order):
    order_id, customer_name, amount, order_date, is_active, postal_code, notes

Design decisions (see REVIEW_NOTES.md for the ones a human should confirm):
  * Dates are read as MM/DD/YYYY. This was inferred from the target sample
    (01/02/2026 -> 2026-01-02, i.e. Jan 2). The source values are all
    day<=12 AND month<=12, so the format is intrinsically ambiguous and is
    resolved only by the target. -> flagged for review.
  * "N/A" and whitespace-only note fields are treated as null -> emptied.
  * Amounts: strip currency symbols and thousands separators; a value wrapped
    in parentheses is negative (accounting notation); format to 2 decimals.
  * customer_name is repaired: mojibake (double UTF-8 encoding) is decoded,
    non-breaking spaces become normal spaces, control characters are stripped,
    and the result is Unicode NFC-normalized to match the target.
  * A row whose entire content is byte-identical to an earlier row is treated
    as an export duplicate and dropped (first occurrence kept). -> flagged.
  * Rows whose order_id is not an 8-digit number (e.g. the "Total" footer and
    the blank line) are excluded as non-data rows. -> reported.
  * A row missing only its trailing notes field is padded (notes = "").
    Any other shape mismatch is quarantined rather than guessed.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

# Source header (after trimming) -> target header.
COLUMN_MAP = {
    "order_id": "order_id",
    "customer": "customer_name",
    "amt": "amount",
    "order_date": "order_date",
    "active": "is_active",
    "zip": "postal_code",
    "notes": "notes",
}
TARGET_HEADER = [
    "order_id",
    "customer_name",
    "amount",
    "order_date",
    "is_active",
    "postal_code",
    "notes",
]
EXPECTED_FIELD_COUNT = len(COLUMN_MAP)

# Values that mean "no value" in a free-text/notes context.
NULL_SENTINELS = {"", "n/a", "na", "null", "none", "nil", "-", "--"}

TRUE_TOKENS = {"y", "yes", "true", "t", "1"}
FALSE_TOKENS = {"n", "no", "false", "f", "0"}

# Characters that, if present in a decoded string, signal likely mojibake
# (the CP1252/Latin-1 lead bytes of a mis-decoded UTF-8 sequence).
MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "Ã\xad", "Ð", "Ñ")


# ----------------------------------------------------------------------------
# Field cleaners (each returns cleaned value + records any fix applied)
# ----------------------------------------------------------------------------


def repair_text(value: str, fixes: Counter) -> str:
    """Clean a human-readable text field (customer names, notes)."""
    # 1. Repair double-encoded UTF-8 ("mojibake"). Only attempted when the
    #    string carries a mojibake marker, so correctly-encoded accented text
    #    (e.g. "José", "Réné") is never touched.
    if any(marker in value for marker in MOJIBAKE_MARKERS):
        try:
            candidate = value.encode("latin-1").decode("utf-8")
            if candidate != value:
                value = candidate
                fixes["ENC: mojibake repaired"] += 1
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass  # not actually latin-1/utf-8 mojibake; leave as-is

    # 2. Normalise exotic spaces (NBSP, thin space, etc.) to a normal space.
    normalized_spaces = "".join(
        " " if (unicodedata.category(ch) == "Zs" and ch != " ") else ch
        for ch in value
    )
    if normalized_spaces != value:
        value = normalized_spaces
        fixes["STR: non-breaking/exotic space normalised"] += 1

    # 3. Strip control characters (e.g. embedded vertical tab 0x0B). We drop
    #    all Unicode "C" (control/format) category characters from name/notes.
    stripped_controls = "".join(
        ch for ch in value if unicodedata.category(ch)[0] != "C"
    )
    if stripped_controls != value:
        value = stripped_controls
        fixes["STR: control character stripped"] += 1

    # 4. Trim outer whitespace and collapse runs of internal whitespace.
    trimmed = " ".join(value.split())
    if trimmed != value:
        value = trimmed
        fixes["STR: whitespace trimmed"] += 1

    # 5. Canonicalise to NFC (target uses precomposed characters).
    nfc = unicodedata.normalize("NFC", value)
    if nfc != value:
        value = nfc
        fixes["STR: Unicode NFC-normalised"] += 1

    return value


def clean_notes(value: str, fixes: Counter) -> str:
    text = repair_text(value, fixes)
    if text.strip().lower() in NULL_SENTINELS:
        if text != "":
            fixes["NUL: sentinel/blank note cleared"] += 1
        return ""
    return text


def parse_amount(value: str, fixes: Counter) -> str:
    """'$1,234.56' -> '1234.56'; '(500)' -> '-500.00'; '1000' -> '1000.00'."""
    raw = value.strip()
    if raw == "":
        raise ValueError("empty amount")

    negative = False
    text = raw
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()
        fixes["NUM: parenthesised negative applied"] += 1

    if any(sym in text for sym in "$£€¥"):
        fixes["NUM: currency symbol removed"] += 1
    if "," in text:
        fixes["NUM: thousands separator removed"] += 1

    cleaned = text.replace(",", "")
    for sym in "$£€¥ ":
        cleaned = cleaned.replace(sym, "")
    if cleaned.startswith("-"):
        negative = True
        cleaned = cleaned[1:]

    try:
        number = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"unparseable amount {raw!r}") from exc
    if negative:
        number = -number

    quantized = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    formatted = f"{quantized:.2f}"
    if formatted != raw:
        fixes["NUM: amount normalised to 2dp"] += 1
    return formatted


def parse_date(value: str, fixes: Counter) -> str:
    """MM/DD/YYYY -> YYYY-MM-DD (format inferred from target sample)."""
    raw = value.strip()
    if raw == "":
        raise ValueError("empty order_date")
    parts = raw.split("/")
    if len(parts) != 3:
        raise ValueError(f"unrecognised date {raw!r}")
    mm, dd, yyyy = parts
    try:
        month, day, year = int(mm), int(dd), int(yyyy)
    except ValueError as exc:
        raise ValueError(f"non-numeric date {raw!r}") from exc
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError(f"out-of-range date {raw!r}")
    iso = f"{year:04d}-{month:02d}-{day:02d}"
    fixes["TYP: date reformatted to ISO-8601"] += 1
    return iso


def parse_bool(value: str, fixes: Counter) -> str:
    token = value.strip().lower()
    if token in TRUE_TOKENS:
        fixes["TYP: boolean normalised"] += 1
        return "true"
    if token in FALSE_TOKENS:
        fixes["TYP: boolean normalised"] += 1
        return "false"
    raise ValueError(f"unrecognised boolean {value!r}")


def clean_postal(value: str, fixes: Counter) -> str:
    """Keep as a string so leading zeros survive (e.g. 02134)."""
    trimmed = value.strip()
    if trimmed != value:
        fixes["STR: whitespace trimmed (postal_code)"] += 1
    return trimmed


def clean_order_id(value: str) -> str:
    return value.strip()


# ----------------------------------------------------------------------------
# Row processing
# ----------------------------------------------------------------------------


def is_data_row(order_id: str) -> bool:
    return order_id.isdigit() and len(order_id) == 8


def transform(input_path: Path, output_path: Path) -> dict:
    fixes: Counter = Counter()
    excluded: list[dict] = []      # non-data rows (footer, blanks)
    quarantined: list[dict] = []   # data-shaped rows we could not transform
    duplicates: list[dict] = []    # exact-duplicate rows dropped
    output_rows: list[list[str]] = []
    seen_rows: set = set()

    total_data_lines = 0

    # utf-8-sig transparently strips the BOM. newline="" lets csv handle the
    # mixed CRLF/LF line endings.
    with input_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        try:
            raw_header = next(reader)
        except StopIteration:
            raise SystemExit("input file is empty")

        src_header = [h.strip() for h in raw_header]
        if src_header != list(COLUMN_MAP.keys()):
            # Not fatal, but worth surfacing loudly.
            print(
                f"WARNING: unexpected source header {src_header!r}; "
                f"expected {list(COLUMN_MAP.keys())!r}",
                file=sys.stderr,
            )

        for line_no, row in enumerate(reader, start=2):
            # Skip genuinely empty lines produced by blank rows.
            if not any(cell.strip() for cell in row):
                excluded.append(
                    {"line": line_no, "reason": "blank line", "raw": row}
                )
                continue

            total_data_lines += 1

            # Normalise field count.
            if len(row) == EXPECTED_FIELD_COUNT - 1:
                # Missing only the trailing notes field -> pad it.
                row = row + [""]
                fixes["ERR: ragged row padded (missing trailing notes)"] += 1
            elif len(row) != EXPECTED_FIELD_COUNT:
                quarantined.append(
                    {
                        "line": line_no,
                        "reason": f"field count {len(row)} != {EXPECTED_FIELD_COUNT}",
                        "raw": row,
                    }
                )
                continue

            order_id = clean_order_id(row[0])

            # Exclude non-data rows (footer totals, stray labels).
            if not is_data_row(order_id):
                excluded.append(
                    {
                        "line": line_no,
                        "reason": "non-data row (order_id not an 8-digit id)",
                        "raw": row,
                    }
                )
                continue

            try:
                out = [
                    order_id,
                    repair_text(row[1], fixes),
                    parse_amount(row[2], fixes),
                    parse_date(row[3], fixes),
                    parse_bool(row[4], fixes),
                    clean_postal(row[5], fixes),
                    clean_notes(row[6], fixes),
                ]
            except ValueError as exc:
                quarantined.append(
                    {"line": line_no, "reason": str(exc), "raw": row}
                )
                continue

            key = tuple(out)
            if key in seen_rows:
                duplicates.append(
                    {"line": line_no, "reason": "exact duplicate", "raw": out}
                )
                fixes["KEY: exact-duplicate row dropped"] += 1
                continue
            seen_rows.add(key)
            output_rows.append(out)

    # Write output (LF line endings, no BOM, to match target).
    with output_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(TARGET_HEADER)
        writer.writerows(output_rows)

    return {
        "input_file": str(input_path),
        "output_file": str(output_path),
        "data_lines_read": total_data_lines,
        "rows_written": len(output_rows),
        "rows_excluded_nondata": len(excluded),
        "rows_quarantined": len(quarantined),
        "duplicate_rows_dropped": len(duplicates),
        "fixes_applied": dict(sorted(fixes.items())),
        "excluded_detail": excluded,
        "quarantined_detail": quarantined,
        "duplicate_detail": duplicates,
    }


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        default=str(here / "eval-inputs" / "orders_export.csv"),
        help="path to the vendor export CSV",
    )
    parser.add_argument(
        "--output",
        default=str(here / "orders_transformed.csv"),
        help="path to write the warehouse-format CSV",
    )
    parser.add_argument(
        "--summary",
        default=str(here / "run_summary.json"),
        help="path to write the machine-readable run summary",
    )
    parser.add_argument(
        "--quarantine",
        default=str(here / "quarantine.csv"),
        help="path to write rejected/excluded rows (only if any exist)",
    )
    args = parser.parse_args()

    summary = transform(Path(args.input), Path(args.output))

    Path(args.summary).write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    rejected = summary["quarantined_detail"] + summary["excluded_detail"]
    if rejected:
        with Path(args.quarantine).open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(["source_line", "reason", "raw_row"])
            for item in rejected:
                w.writerow([item["line"], item["reason"], json.dumps(item["raw"], ensure_ascii=False)])

    # Human-readable console summary.
    print("ETL run complete")
    print(f"  input                : {summary['input_file']}")
    print(f"  output               : {summary['output_file']}")
    print(f"  data lines read      : {summary['data_lines_read']}")
    print(f"  rows written         : {summary['rows_written']}")
    print(f"  excluded (non-data)  : {summary['rows_excluded_nondata']}")
    print(f"  quarantined          : {summary['rows_quarantined']}")
    print(f"  duplicates dropped   : {summary['duplicate_rows_dropped']}")
    print("  fixes applied:")
    for name, count in summary["fixes_applied"].items():
        print(f"      {count:>3}  {name}")

    # data_lines_read excludes blank lines; excluded includes blank lines, so
    # reconcile against read + blank lines.
    blanks = sum(1 for e in summary["excluded_detail"] if e["reason"] == "blank line")
    reconciled = summary["rows_written"] + summary["rows_quarantined"] + \
        summary["duplicate_rows_dropped"] + \
        (summary["rows_excluded_nondata"] - blanks)
    if reconciled != summary["data_lines_read"]:
        print(
            f"  WARNING: accounting mismatch: {reconciled} != "
            f"{summary['data_lines_read']}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
