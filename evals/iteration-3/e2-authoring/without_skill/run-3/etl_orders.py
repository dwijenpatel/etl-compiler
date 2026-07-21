#!/usr/bin/env python3
"""
ETL: vendor orders_export.csv  ->  warehouse orders format.

Transforms the vendor CSV export to match the warehouse target schema
(see eval-inputs/target_orders.csv for the desired shape).

Stdlib only. Run:  python3 etl_orders.py

Design notes / decisions the author made (see README section at bottom and
the generated run report for the review-worthy ones):

  Source header        -> Target header
  ---------------------------------------------
  order_id             -> order_id
  " customer " (trim)  -> customer_name
  amt                  -> amount
  order_date           -> order_date
  active               -> is_active
  zip                  -> postal_code
  notes                -> notes

Per-field cleaning is documented inline. Every automatic fix is counted and
reported. Rows that cannot be safely interpreted are quarantined (written to
orders_rejected.csv) rather than guessed at. Nothing is silently dropped.
"""

import csv
import io
import os
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "eval-inputs", "orders_export.csv")
OUT_DIR = os.path.join(HERE, "output")
OUT_CLEAN = os.path.join(OUT_DIR, "orders_transformed.csv")
OUT_REJECT = os.path.join(OUT_DIR, "orders_rejected.csv")
OUT_REPORT = os.path.join(OUT_DIR, "orders_run_report.txt")

TARGET_HEADER = [
    "order_id", "customer_name", "amount", "order_date",
    "is_active", "postal_code", "notes",
]

# Free-text values that mean "no value" (case-insensitive, after trimming).
# Kept deliberately small: only sentinels observed / low-risk. "N/A" is
# confirmed by the target sample (row 3 -> empty notes).
NULL_SENTINELS = {"", "n/a", "na", "null", "none", "nil"}

# Boolean mapping for the active flag.
TRUE_TOKENS = {"y", "yes", "true", "t", "1"}
FALSE_TOKENS = {"n", "no", "false", "f", "0"}

# Accepted input date format. All source values are MM/DD/YYYY and the target
# sample confirms the MM/DD ordering (01/02 -> 2026-01-02, 03/04 -> 2026-03-04).
IN_DATE_FMT = "%m/%d/%Y"
OUT_DATE_FMT = "%Y-%m-%d"

fixes = Counter()          # auto-fix accounting, by category
review = []                # human review items (list of str)


# --------------------------------------------------------------------------
# Field cleaners
# --------------------------------------------------------------------------

def fix_mojibake(s):
    """Repair UTF-8-decoded-as-Latin-1 mojibake (e.g. 'JosÃ©' -> 'José').

    Only attempts a repair when classic mojibake markers are present, and only
    accepts the result if the latin-1/utf-8 round-trip succeeds cleanly. Correct
    text like 'José' fails the round-trip and is therefore left untouched.
    Loops (capped) to undo double-encoding.
    """
    out = s
    for _ in range(3):
        if not any(m in out for m in ("Ã", "Â", "â\x80")):
            break
        try:
            candidate = out.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if candidate == out:
            break
        out = candidate
    if out != s:
        fixes["ENC mojibake repaired"] += 1
    return out


def clean_text(s, *, field, order_id):
    """Normalize a free-text field: mojibake, Unicode form, spaces, controls."""
    if s is None:
        return ""
    original = s
    s = fix_mojibake(s)

    # Precomposed Unicode (combining accents -> single code point), e.g. the
    # NFD 'Réné' in the source becomes precomposed to match the target.
    nfc = unicodedata.normalize("NFC", s)
    if nfc != s:
        fixes["ENC normalized to NFC"] += 1
    s = nfc

    # Map any Unicode space separator (incl. NBSP U+00A0) to a plain space.
    def _space(ch):
        return " " if unicodedata.category(ch) == "Zs" else ch
    mapped = "".join(_space(ch) for ch in s)
    if mapped != s:
        fixes["STR non-breaking/odd space normalized"] += 1
    s = mapped

    # Drop control / format characters (e.g. the vertical tab inside "BobSmith").
    stripped_ctrl = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cc", "Cf"))
    if stripped_ctrl != s:
        fixes["STR control char removed"] += 1
        review.append(
            f"order_id={order_id}: {field} contained a control character; it was "
            f"removed (raw={original!r} -> {stripped_ctrl.strip()!r}). If this was "
            f"meant to be a word separator the name may need a space instead."
        )
    s = stripped_ctrl

    # Collapse internal whitespace runs and trim ends.
    collapsed = re.sub(r"\s+", " ", s).strip()
    if collapsed != s:
        fixes["STR whitespace trimmed/collapsed"] += 1
    return collapsed


def clean_amount(raw):
    """Parse a messy currency string to a fixed-2-decimal number string.

    Handles $, thousands commas, surrounding whitespace, and accounting-style
    negatives '(500)'. Returns (value_str, None) or (None, error_reason).
    """
    if raw is None:
        return None, "amount is missing"
    s = raw.strip()
    if s == "":
        return None, "amount is empty"
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
        fixes["TYP accounting-negative parsed"] += 1
    cleaned = s.replace("$", "").replace(",", "").strip()
    if cleaned != s:
        fixes["TYP currency symbol/grouping stripped"] += 1
    if cleaned.startswith("-"):
        negative = True
        cleaned = cleaned[1:].strip()
    try:
        val = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None, f"amount not numeric: {raw!r}"
    if negative:
        val = -val
    return str(val.quantize(Decimal("0.01"))), None


def clean_date(raw):
    """MM/DD/YYYY -> YYYY-MM-DD. Returns (value_str, None) or (None, reason)."""
    if raw is None or raw.strip() == "":
        return None, "date is empty"
    s = raw.strip()
    try:
        dt = datetime.strptime(s, IN_DATE_FMT)
    except ValueError:
        return None, f"date not in MM/DD/YYYY form: {raw!r}"
    out = dt.strftime(OUT_DATE_FMT)
    fixes["TYP date reformatted to ISO"] += 1
    return out, None


def clean_active(raw):
    """Y/N (and common synonyms) -> true/false. Returns (val, None) or (None, reason)."""
    if raw is None:
        return None, "active flag missing"
    t = raw.strip().lower()
    if t in TRUE_TOKENS:
        fixes["TYP boolean mapped"] += 1
        return "true", None
    if t in FALSE_TOKENS:
        fixes["TYP boolean mapped"] += 1
        return "false", None
    return None, f"active flag not recognized: {raw!r}"


def clean_notes(raw):
    if raw is None:
        return ""
    s = raw.strip()
    if s.lower() in NULL_SENTINELS:
        if s != "":
            fixes["NUL sentinel -> empty"] += 1
        return ""
    return s


def clean_postal(raw):
    """Keep as string to preserve leading zeros (e.g. 02134)."""
    if raw is None:
        return ""
    s = raw.strip()
    return s


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    # utf-8-sig strips the BOM if present.
    with open(SRC, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))

    if not rows:
        print("ERROR: source is empty", file=sys.stderr)
        return 1

    fixes["header BOM stripped"] += 1  # utf-8-sig handled it
    src_header = [h.strip() for h in rows[0]]
    fixes["header whitespace trimmed"] += 1
    n_src_cols = len(src_header)

    clean_rows = []
    rejected = []  # (line_no, reason, raw_fields)
    stats = Counter()
    seen_full = {}       # full output-tuple -> first line_no
    seen_order_id = {}   # order_id -> first line_no

    for idx, fields in enumerate(rows[1:], start=2):  # source line number (1-based, header=1)
        stats["source_rows_read"] += 1

        # Blank line?
        if len(fields) == 0 or all(c.strip() == "" for c in fields):
            stats["blank_rows_skipped"] += 1
            fixes["blank row skipped"] += 1
            continue

        # Ragged rows: pad short, quarantine over-long.
        if len(fields) < n_src_cols:
            fixes["ragged short row padded"] += 1
            review.append(
                f"source line {idx}: row had {len(fields)} fields "
                f"(expected {n_src_cols}); missing trailing field(s) padded empty. "
                f"raw={fields!r}"
            )
            fields = fields + [""] * (n_src_cols - len(fields))
        elif len(fields) > n_src_cols:
            rejected.append((idx, f"too many fields ({len(fields)} > {n_src_cols})", fields))
            stats["rows_quarantined"] += 1
            continue

        rec = dict(zip(src_header, fields))
        order_id = (rec.get("order_id") or "").strip()

        # Footer / summary rows: non-numeric order_id (e.g. "Total").
        if not re.fullmatch(r"\d+", order_id):
            rejected.append((idx, f"non-numeric order_id ({order_id!r}); looks like a footer/summary row", fields))
            stats["rows_quarantined"] += 1
            fixes["footer/non-data row excluded"] += 1
            continue

        # Transform fields; collect any hard errors for quarantine.
        errors = []
        amount, err = clean_amount(rec.get("amt"))
        if err:
            errors.append(err)
        order_date, err = clean_date(rec.get("order_date"))
        if err:
            errors.append(err)
        is_active, err = clean_active(rec.get("active"))
        if err:
            errors.append(err)

        if errors:
            rejected.append((idx, "; ".join(errors), fields))
            stats["rows_quarantined"] += 1
            continue

        out_row = [
            order_id,
            clean_text(rec.get("customer"), field="customer_name", order_id=order_id),
            amount,
            order_date,
            is_active,
            clean_postal(rec.get("zip")),
            clean_notes(rec.get("notes")),
        ]

        # Duplicate detection (kept, not dropped — see review notes).
        key = tuple(out_row)
        if key in seen_full:
            stats["exact_duplicate_rows"] += 1
            review.append(
                f"order_id={order_id}: EXACT duplicate of the row from source line "
                f"{seen_full[key]} (source line {idx}). Both copies were KEPT. If the "
                f"warehouse treats order_id as a unique key, decide whether to drop one."
            )
        else:
            seen_full[key] = idx
        if order_id in seen_order_id:
            stats["duplicate_order_ids"] += 1
        else:
            seen_order_id[order_id] = idx

        clean_rows.append(out_row)
        stats["rows_written"] += 1

    # ---- write clean output (utf-8, no BOM, LF, minimal quoting) ----
    with open(OUT_CLEAN, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(TARGET_HEADER)
        w.writerows(clean_rows)

    # ---- write rejected/quarantined rows with reasons ----
    with open(OUT_REJECT, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["source_line", "reason", "raw_row"])
        for line_no, reason, raw in rejected:
            w.writerow([line_no, reason, "|".join(raw)])

    # ---- write human-readable run report ----
    write_report(stats, rejected)

    # Console summary
    print(f"Read {stats['source_rows_read']} data rows from {os.path.relpath(SRC, HERE)}")
    print(f"  written  : {stats['rows_written']} -> {os.path.relpath(OUT_CLEAN, HERE)}")
    print(f"  quarantined: {stats['rows_quarantined']} -> {os.path.relpath(OUT_REJECT, HERE)}")
    print(f"  blank skipped: {stats['blank_rows_skipped']}")
    print(f"  exact duplicates kept: {stats['exact_duplicate_rows']}")
    print(f"  review items: {len(review)} (see {os.path.relpath(OUT_REPORT, HERE)})")

    # Exit code: 0 clean, 2 completed-with-quarantine/review.
    if stats["rows_quarantined"] or review:
        return 2
    return 0


def write_report(stats, rejected):
    lines = []
    lines.append("ETL RUN REPORT — vendor orders_export.csv -> warehouse orders")
    lines.append("=" * 64)
    lines.append(f"generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("COUNTS")
    lines.append("-" * 64)
    lines.append(f"  source data rows read : {stats['source_rows_read']}")
    lines.append(f"  rows written (clean)  : {stats['rows_written']}")
    lines.append(f"  rows quarantined      : {stats['rows_quarantined']}")
    lines.append(f"  blank rows skipped    : {stats['blank_rows_skipped']}")
    lines.append(f"  exact duplicate rows  : {stats['exact_duplicate_rows']} (kept, not dropped)")
    lines.append(f"  duplicate order_ids   : {stats['duplicate_order_ids']}")
    lines.append("")
    lines.append("AUTO-FIXES APPLIED (counted)")
    lines.append("-" * 64)
    for name in sorted(fixes):
        lines.append(f"  {name:<40} {fixes[name]}")
    lines.append("")
    lines.append("QUARANTINED ROWS (see orders_rejected.csv)")
    lines.append("-" * 64)
    if rejected:
        for line_no, reason, raw in rejected:
            lines.append(f"  line {line_no}: {reason}")
            lines.append(f"           raw: {'|'.join(raw)}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("REVIEW WHEN YOU ARE BACK  (choices I made without you)")
    lines.append("-" * 64)
    review_block = [
        "1. DATE FORMAT: all source dates are MM/DD/YYYY. Every value happens to "
        "have day<=12 too, so in isolation they are ambiguous (MM/DD vs DD/MM). "
        "I used MM/DD/YYYY because your target sample confirms it "
        "(01/02/2026 -> 2026-01-02, 03/04/2026 -> 2026-03-04). Confirm this is right.",
        "2. DUPLICATES: order_id 10000005 appears twice (identical rows). I KEPT "
        "both copies rather than silently dropping one. If order_id is a unique key "
        "in the warehouse, you'll want to dedupe one of them.",
        "3. 'Total' FOOTER ROW: the last data row was 'Total,,3222.56,...'. I treated "
        "any non-numeric order_id as a non-data/footer row and excluded it "
        "(quarantined, not in the clean output).",
        "4. MOJIBAKE REPAIR: one customer name was double-encoded ('JosÃ© GarcÃ­a'). "
        "I repaired it to 'José García' to match your sample. Correct names were "
        "left untouched.",
        "5. CONTROL CHAR IN NAME: one name had an embedded control character "
        "(vertical tab), which I removed, yielding 'BobSmith'. If it was meant to "
        "separate words it may need to be 'Bob Smith' — please confirm.",
        "6. NOTES SENTINELS: 'N/A' and whitespace-only notes were converted to empty "
        "(matches your sample). Other free-text is preserved verbatim.",
        "7. RAGGED ROW: order 10000006 was missing its trailing notes field; I padded "
        "notes to empty and kept the row.",
    ]
    for item in review_block:
        lines.append(f"  {item}")
    lines.append("")
    lines.append("  Row-specific notes:")
    if review:
        for r in review:
            lines.append(f"    - {r}")
    else:
        lines.append("    (none)")
    lines.append("")

    with open(OUT_REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
