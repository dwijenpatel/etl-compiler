#!/usr/bin/env python3
"""
ETL: vendor orders_export.csv  ->  warehouse orders format.

Transforms the vendor export to match the warehouse target schema, which was
reverse-engineered from eval-inputs/target_orders.csv (a 3-row sample of the
desired output). Stdlib-only, deterministic, no external dependencies.

Target schema (exact header):
    order_id,customer_name,amount,order_date,is_active,postal_code,notes

Run:
    python3 etl_orders.py

Outputs (see output/ dir):
    orders_warehouse.csv  - clean rows conformant to the target schema
    rejects.csv           - quarantined rows (ragged / conflicting), with reason + raw text
    run_report.json       - machine-readable run summary: counts + per-fix tallies + review flags

Every meaning-changing decision is either (a) confirmed by the target sample, or
(b) flagged in REVIEW.md and run_report.json["review_required"] for a human to confirm.
"""

import csv
import io
import json
import os
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "eval-inputs", "orders_export.csv")
OUT_DIR = os.path.join(HERE, "output")

TARGET_HEADER = [
    "order_id", "customer_name", "amount", "order_date",
    "is_active", "postal_code", "notes",
]

# Source header (after BOM strip + per-name trim) -> target field.
COLUMN_MAP = {
    "order_id": "order_id",
    "customer": "customer_name",   # source header is " customer " (padded)
    "amt": "amount",
    "order_date": "order_date",
    "active": "is_active",
    "zip": "postal_code",
    "notes": "notes",
}

# Tokens (case-insensitive, trimmed) that mean "no value" in a notes cell.
NULL_TOKENS = {"", "n/a", "na", "null", "none", "-", "--"}

# If any of these appear, a cell is a candidate for double-encoded UTF-8 repair.
MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "Ð", "Ñ", "�")


class FixTally:
    """Counts auto-fixes applied, per fix-type, per column."""
    def __init__(self):
        self.counts = Counter()

    def bump(self, fix_type, column):
        self.counts[(fix_type, column)] += 1

    def as_list(self):
        return [
            {"fix": ft, "column": col, "count": n}
            for (ft, col), n in sorted(self.counts.items())
        ]


def repair_mojibake(s, tally, col):
    """
    Repair double-encoded UTF-8 (e.g. 'JosÃ©' -> 'José').

    Only attempts repair when a mojibake marker is present AND the latin-1 ->
    utf-8 round trip both succeeds and changes the string. Correctly-encoded
    text (e.g. 'José', 'Réné') fails the round trip and is left untouched.
    Returns (repaired_string, was_repaired: bool).
    """
    if not any(m in s for m in MOJIBAKE_MARKERS):
        return s, False
    try:
        candidate = s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s, False
    if candidate != s and "�" not in candidate:
        tally.bump("mojibake_repair", col)
        return candidate, True
    return s, False


def clean_name(raw, tally):
    s = raw.strip()
    s, _ = repair_mojibake(s, tally, "customer_name")
    normalized = unicodedata.normalize("NFC", s)
    if normalized != s:
        tally.bump("unicode_nfc_normalize", "customer_name")
    return normalized


def parse_amount(raw, tally):
    """
    '$1,234.56' -> '1234.56' ; '(500)' -> '-500.00' ; '1000' -> '1000.00'.
    Handles $, thousands separators, and accounting-parenthesis negatives.
    Returns (formatted_string, error_or_None).
    """
    s = raw.strip()
    if s == "":
        return None, "empty amount"
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
        tally.bump("accounting_negative", "amount")
    cleaned = s.replace("$", "").replace(",", "").replace(" ", "").strip()
    if cleaned.startswith("-"):
        negative = True
        cleaned = cleaned[1:]
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    if raw.strip() != cleaned and not negative:
        tally.bump("amount_cleaned", "amount")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None, "unparseable amount: %r" % raw
    if negative:
        value = -value
    return "%0.2f" % value, None


def parse_date(raw, tally):
    """MM/DD/YYYY -> YYYY-MM-DD (US format, confirmed by the target sample)."""
    s = raw.strip()
    if s == "":
        return None, "empty date"
    try:
        dt = datetime.strptime(s, "%m/%d/%Y")
    except ValueError:
        return None, "unparseable date (expected MM/DD/YYYY): %r" % raw
    tally.bump("date_reformat", "order_date")
    return dt.strftime("%Y-%m-%d"), None


def parse_active(raw, tally):
    s = raw.strip().lower()
    if s in ("y", "yes", "true", "1", "t"):
        tally.bump("bool_normalize", "is_active")
        return "true", None
    if s in ("n", "no", "false", "0", "f"):
        tally.bump("bool_normalize", "is_active")
        return "false", None
    return None, "unrecognized active flag: %r" % raw


def clean_postal(raw, tally):
    """Keep as string; preserve leading zeros (never coerce to int)."""
    return raw.strip()


def clean_notes(raw, tally):
    s = raw.strip()
    if s.lower() in NULL_TOKENS:
        if raw.strip() != "":
            tally.bump("null_token_to_empty", "notes")
        return ""
    return s


def transform_row(src, tally):
    """
    src: dict keyed by target field name. Returns (out_list, error_or_None).
    A non-None error means the row should be quarantined.
    """
    order_id = src["order_id"].strip()
    if not order_id.isdigit():
        return None, "non-numeric order_id (footer/summary?): %r" % src["order_id"]

    amount, err = parse_amount(src["amount"], tally)
    if err:
        return None, err
    order_date, err = parse_date(src["order_date"], tally)
    if err:
        return None, err
    is_active, err = parse_active(src["is_active"], tally)
    if err:
        return None, err

    out = [
        order_id,
        clean_name(src["customer_name"], tally),
        amount,
        order_date,
        is_active,
        clean_postal(src["postal_code"], tally),
        clean_notes(src["notes"], tally),
    ]
    return out, None


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    tally = FixTally()

    # Read raw bytes; strip a UTF-8 BOM if present, decode as UTF-8.
    with open(SRC, "rb") as fh:
        raw_bytes = fh.read()
    had_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    text = raw_bytes.decode("utf-8-sig")  # utf-8-sig transparently drops the BOM

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        print("ERROR: empty input", file=sys.stderr)
        return 1

    # Header: trim each name, map to target fields.
    src_header = [h.strip() for h in rows[0]]
    unknown = [h for h in src_header if h not in COLUMN_MAP]
    if unknown:
        print("ERROR: unmapped source columns: %r" % unknown, file=sys.stderr)
        return 1
    field_order = [COLUMN_MAP[h] for h in src_header]

    good_rows = []
    rejects = []            # (reason, raw_line_number, raw_fields)
    blank_skipped = 0
    footer_skipped = 0
    duplicates_dropped = []  # order_ids
    conflicts = []           # order_ids with same id but different content
    seen = {}                # order_id -> transformed out_list

    for lineno, fields in enumerate(rows[1:], start=2):
        # Fully blank line (0 fields or all-empty).
        if len(fields) == 0 or all(c.strip() == "" for c in fields):
            blank_skipped += 1
            continue

        # Ragged: wrong number of columns -> quarantine, never pad/shift silently.
        if len(fields) != len(src_header):
            first = fields[0].strip() if fields else ""
            if not first.isdigit():
                # e.g. "Total,,3222.56,,,," — a summary/footer, not a data row.
                footer_skipped += 1
                rejects.append(("footer/summary row (non-numeric id, wrong arity)",
                                lineno, fields))
                continue
            rejects.append(("ragged row: %d fields, expected %d"
                            % (len(fields), len(src_header)), lineno, fields))
            continue

        src = dict(zip(field_order, fields))
        out, err = transform_row(src, tally)
        if err:
            if "footer" in err or "non-numeric order_id" in err:
                footer_skipped += 1
            rejects.append((err, lineno, fields))
            continue

        oid = out[0]
        if oid in seen:
            if out == seen[oid]:
                duplicates_dropped.append(oid)
                rejects.append(("exact duplicate of an earlier row "
                                "(same order_id + identical content); kept first",
                                lineno, fields))
                continue
            else:
                conflicts.append(oid)
                rejects.append(("order_id conflict: id %s already seen with "
                                "different content; quarantined" % oid, lineno, fields))
                continue
        seen[oid] = out
        good_rows.append(out)

    # ---- write outputs ----
    out_csv = os.path.join(OUT_DIR, "orders_warehouse.csv")
    with open(out_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(TARGET_HEADER)
        w.writerows(good_rows)

    rejects_csv = os.path.join(OUT_DIR, "rejects.csv")
    with open(rejects_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["reason", "source_line", "raw_fields"])
        for reason, lineno, fields in rejects:
            w.writerow([reason, lineno, " | ".join(fields)])

    report = {
        "source": os.path.relpath(SRC, HERE),
        "output": os.path.relpath(out_csv, HERE),
        "input_had_utf8_bom": had_bom,
        "counts": {
            "data_rows_in": len(rows) - 1,
            "rows_written": len(good_rows),
            "blank_rows_skipped": blank_skipped,
            "footer_rows_skipped": footer_skipped,
            "exact_duplicates_dropped": len(duplicates_dropped),
            "order_id_conflicts_quarantined": len(conflicts),
            "rows_rejected_total": len(rejects),
        },
        "auto_fixes": tally.as_list(),
        "review_required": [],
    }

    if duplicates_dropped:
        report["review_required"].append({
            "issue": "exact_duplicate_rows",
            "detail": "order_id(s) %s appeared more than once with identical content. "
                      "Kept the first occurrence and dropped the rest, because order_id "
                      "is the warehouse primary key and would collide. Confirm the dupes "
                      "are export artifacts, not real distinct orders."
                      % ", ".join(sorted(set(duplicates_dropped))),
        })
    if conflicts:
        report["review_required"].append({
            "issue": "order_id_conflicts",
            "detail": "order_id(s) %s appeared more than once with DIFFERENT content; "
                      "all quarantined to rejects.csv rather than guessing which is right."
                      % ", ".join(sorted(set(conflicts))),
        })
    ragged = [r for r in rejects if r[0].startswith("ragged")]
    if ragged:
        report["review_required"].append({
            "issue": "ragged_rows",
            "detail": "%d row(s) had the wrong number of columns and were quarantined to "
                      "rejects.csv rather than padded or column-shifted. See source lines %s."
                      % (len(ragged), ", ".join(str(r[1]) for r in ragged)),
        })
    if any(f["fix"] == "mojibake_repair" for f in report["auto_fixes"]):
        report["review_required"].append({
            "issue": "mojibake_repaired",
            "detail": "Some customer names were double-encoded UTF-8 (e.g. 'JosÃ©') and were "
                      "auto-repaired to 'José'-style text (matches the target sample). "
                      "Spot-check the repaired names.",
        })
    report["review_required"].append({
        "issue": "date_format_assumption",
        "detail": "Source dates parsed as US MM/DD/YYYY (confirmed by the target sample rows). "
                  "If any vendor batch ever uses DD/MM/YYYY this would misread; all sample "
                  "dates conform to MM/DD.",
    })

    report_json = os.path.join(OUT_DIR, "run_report.json")
    with open(report_json, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)

    # ---- console summary ----
    c = report["counts"]
    print("ETL complete.")
    print("  in:  %s (BOM=%s)" % (report["source"], had_bom))
    print("  out: %s" % report["output"])
    print("  rows written:            %d" % c["rows_written"])
    print("  blank skipped:           %d" % c["blank_rows_skipped"])
    print("  footer/summary skipped:  %d" % c["footer_rows_skipped"])
    print("  exact duplicates dropped:%d" % c["exact_duplicates_dropped"])
    print("  id conflicts quarantined:%d" % c["order_id_conflicts_quarantined"])
    print("  total rejected:          %d  -> output/rejects.csv" % c["rows_rejected_total"])
    print("  auto-fixes:")
    for f in report["auto_fixes"]:
        print("      %-22s %-14s x%d" % (f["fix"], f["column"], f["count"]))
    print("  review items:            %d  -> output/run_report.json / REVIEW.md"
          % len(report["review_required"]))

    # Exit code: 0 clean, 2 if anything needs review/was quarantined.
    return 2 if report["review_required"] or rejects else 0


if __name__ == "__main__":
    sys.exit(main())
