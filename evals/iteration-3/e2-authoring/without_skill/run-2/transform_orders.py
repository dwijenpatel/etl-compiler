#!/usr/bin/env python3
"""
transform_orders.py

Transforms the vendor CSV export (eval-inputs/orders_export.csv) into the
warehouse format demonstrated by eval-inputs/target_orders.csv.

Stdlib only. Run:  python3 transform_orders.py

The transformation rules were derived by diffing the vendor export against the
target sample. Every non-trivial choice is documented in REVIEW.md and logged to
stdout. Anything that could not be cleanly transformed is written to
output/orders_rejects.csv with a reason (never silently dropped).

Target schema (order + names taken from target_orders.csv):
    order_id, customer_name, amount, order_date, is_active, postal_code, notes
"""

import csv
import re
import sys
import unicodedata
from decimal import Decimal, InvalidOperation
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
SRC = BASE / "eval-inputs" / "orders_export.csv"
OUT_DIR = BASE / "output"
OUT = OUT_DIR / "orders_warehouse.csv"
REJECTS = OUT_DIR / "orders_rejects.csv"

TARGET_FIELDS = [
    "order_id", "customer_name", "amount",
    "order_date", "is_active", "postal_code", "notes",
]

# Normalized (lower/trimmed) source header -> target field.
COLUMN_MAP = {
    "order_id": "order_id",
    "customer": "customer_name",
    "amt": "amount",
    "order_date": "order_date",
    "active": "is_active",
    "zip": "postal_code",
    "notes": "notes",
}

# Tokens (case-insensitive, trimmed) treated as "no value".
NULL_TOKENS = {"", "n/a", "na", "null", "none", "-", "--"}

TRUE_TOKENS = {"y", "yes", "true", "t", "1"}
FALSE_TOKENS = {"n", "no", "false", "f", "0"}

# Presence of any of these strongly suggests UTF-8-decoded-as-Latin-1 damage.
MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "Ð", "Ñ", " Â", "�")


# --------------------------------------------------------------------------- #
# Field cleaners
# --------------------------------------------------------------------------- #

def repair_mojibake(s: str) -> str:
    """Repair classic 'JosÃ©' -> 'José' damage.

    Only attempts a repair when a mojibake marker is present AND the latin-1 ->
    utf-8 round-trip decodes cleanly, so already-correct text (e.g. 'José') is
    left untouched.
    """
    if not any(m in s for m in MOJIBAKE_MARKERS):
        return s
    try:
        repaired = s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    return repaired if repaired != s else s


def normalize_unicode(s: str) -> str:
    """Normalize to NFC so decomposed accents (e.g. 'e'+U+0301) become the
    precomposed 'é' the target uses. The source mixes NFC and NFD forms."""
    return unicodedata.normalize("NFC", s)


def clean_text(raw: str) -> str:
    return normalize_unicode(repair_mojibake(raw.strip()))


def clean_notes(raw: str):
    """Trim, repair encoding, NFC-normalize, and map null-ish tokens to empty."""
    s = normalize_unicode(repair_mojibake(raw.strip()))
    if s.lower() in NULL_TOKENS:
        return "", None
    return s, None


def clean_amount(raw: str):
    """Return (value_str, warning). Handles '$', thousands ',', and
    accounting-style '(500)' negatives. Formats to 2 decimal places."""
    s = raw.strip()
    if s.lower() in NULL_TOKENS:
        return "", "empty amount"
    neg = False
    m = re.match(r"^\((.*)\)$", s)          # (500) -> -500
    if m:
        neg = True
        s = m.group(1).strip()
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    if s.startswith("-"):
        neg = True
        s = s[1:]
    try:
        val = Decimal(s)
    except InvalidOperation:
        return None, f"unparseable amount {raw!r}"
    if neg:
        val = -val
    return f"{val:.2f}", None


def clean_date(raw: str):
    """Vendor uses MM/DD/YYYY (confirmed by target sample: 01/02/2026 ->
    2026-01-02). Returns (iso_str, warning)."""
    s = raw.strip()
    if s.lower() in NULL_TOKENS:
        return "", "empty date"
    try:
        return datetime.strptime(s, "%m/%d/%Y").strftime("%Y-%m-%d"), None
    except ValueError:
        pass
    # Fallbacks (flagged): already-ISO, 2-digit year, or D/M/Y.
    for fmt in ("%Y-%m-%d", "%m/%d/%y"):
        try:
            iso = datetime.strptime(s, fmt).strftime("%Y-%m-%d")
            return iso, f"non-standard date format {raw!r} parsed as {fmt}"
        except ValueError:
            continue
    return None, f"unparseable date {raw!r}"


def clean_bool(raw: str):
    s = raw.strip().lower()
    if s in TRUE_TOKENS:
        return "true", None
    if s in FALSE_TOKENS:
        return "false", None
    if s in NULL_TOKENS:
        return "", "empty active flag"
    return None, f"unrecognized boolean {raw!r}"


def clean_postal(raw: str):
    """Keep as string to preserve leading zeros (e.g. 02134)."""
    s = raw.strip()
    if s.lower() in NULL_TOKENS:
        return "", None
    warn = None if re.fullmatch(r"\d{5}(-\d{4})?", s) else f"unusual postal_code {raw!r}"
    return s, warn


# --------------------------------------------------------------------------- #
# Row classification
# --------------------------------------------------------------------------- #

def classify_row(row):
    """Return ('data'|'blank'|'footer', reason)."""
    if not any(c.strip() for c in row):
        return "blank", "blank line"
    if not row[0].strip().isdigit():
        return "footer", f"non-numeric order_id {row[0].strip()!r} (summary/footer)"
    return "data", None


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)

    stats = {
        "total_lines": 0, "written": 0, "blank_skipped": 0,
        "footer_skipped": 0, "rejected": 0, "exact_dupes_dropped": 0,
        "ragged_padded": 0,
    }
    warnings = []          # (order_id, field, message)
    rejects = []           # (reason, raw_row)
    review = []            # high-level items for the human to review
    seen_full = set()      # exact-duplicate detection (raw tuple)
    seen_ids = {}          # order_id -> first raw tuple (differing-dup detection)
    out_rows = []

    with SRC.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            print("ERROR: empty source file", file=sys.stderr)
            return 1

        norm_header = [h.strip().lower() for h in header]
        # Map source position -> target field.
        pos_to_target = {}
        for i, h in enumerate(norm_header):
            if h in COLUMN_MAP:
                pos_to_target[i] = COLUMN_MAP[h]
        mapped_targets = set(pos_to_target.values())
        missing = [f for f in TARGET_FIELDS if f not in mapped_targets]
        if missing:
            review.append(f"Source header did not map to target fields: {missing}. "
                          f"Raw header was {header!r}.")
        n_expected = len(header)

        for row in reader:
            stats["total_lines"] += 1
            kind, reason = classify_row(row)
            if kind == "blank":
                stats["blank_skipped"] += 1
                continue
            if kind == "footer":
                stats["footer_skipped"] += 1
                review.append(f"Skipped footer/summary line: {row!r} ({reason}). "
                              "Vendor's own Total (3222.56) is unreliable: it counts "
                              "the duplicate row and treats '(500)' as +500, so it was "
                              "excluded, not reconciled.")
                continue

            # Normalize row width to the header width.
            if len(row) < n_expected:
                stats["ragged_padded"] += 1
                oid = row[0].strip() if row else "?"
                warnings.append((oid, "row", f"ragged row padded "
                                 f"({len(row)} of {n_expected} cols); missing "
                                 "trailing fields set empty"))
                row = row + [""] * (n_expected - len(row))
            elif len(row) > n_expected:
                stats["rejected"] += 1
                rejects.append((f"too many columns "
                                f"({len(row)} > {n_expected})", row))
                continue

            key = tuple(row)
            if key in seen_full:
                stats["exact_dupes_dropped"] += 1
                oid = row[0].strip()
                review.append(f"Dropped exact-duplicate row for order_id {oid} "
                              "(all fields identical -> treated as a double-export "
                              "artifact; order_id is the warehouse key).")
                continue
            seen_full.add(key)

            oid_raw = row[0].strip()
            if oid_raw in seen_ids and seen_ids[oid_raw] != key:
                warnings.append((oid_raw, "order_id",
                                 "duplicate order_id with DIFFERING data — kept both, "
                                 "needs manual resolution"))
                review.append(f"order_id {oid_raw} appears more than once with "
                              "different data; both rows were kept for review.")
            seen_ids.setdefault(oid_raw, key)

            # Build the target record.
            src = {pos_to_target[i]: row[i] for i in pos_to_target}
            rec = {f: "" for f in TARGET_FIELDS}
            row_errors = []

            rec["order_id"] = src.get("order_id", "").strip()
            rec["customer_name"] = clean_text(src.get("customer_name", ""))

            for field, cleaner, key_name in (
                ("amount", clean_amount, "amount"),
                ("order_date", clean_date, "order_date"),
                ("is_active", clean_bool, "is_active"),
                ("postal_code", clean_postal, "postal_code"),
                ("notes", clean_notes, "notes"),
            ):
                val, warn = cleaner(src.get(key_name, ""))
                if val is None:
                    row_errors.append(warn)
                else:
                    rec[field] = val
                    if warn:
                        warnings.append((rec["order_id"], field, warn))

            if row_errors:
                stats["rejected"] += 1
                rejects.append(("; ".join(row_errors), row))
                continue

            out_rows.append(rec)
            stats["written"] += 1

    # Write warehouse output (atomic-ish: write then it's complete).
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=TARGET_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(out_rows)

    # Write rejects (with reason), even if empty (so the artifact always exists).
    with REJECTS.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["reject_reason", "raw_row"])
        for reason, raw in rejects:
            w.writerow([reason, "|".join(raw)])

    # ---- Console report ---------------------------------------------------- #
    print("=" * 64)
    print("ETL run: orders_export.csv -> orders_warehouse.csv")
    print("=" * 64)
    for k in ("total_lines", "written", "blank_skipped", "footer_skipped",
              "ragged_padded", "exact_dupes_dropped", "rejected"):
        print(f"  {k:22s}: {stats[k]}")
    print(f"  output                : {OUT}")
    print(f"  rejects               : {REJECTS} ({len(rejects)} rows)")

    if warnings:
        print("\nAuto-fixes / warnings (per row):")
        for oid, field, msg in warnings:
            print(f"  [order {oid}] {field}: {msg}")

    if review:
        print("\nREVIEW ITEMS (see REVIEW.md):")
        for item in review:
            print(f"  - {item}")

    # Exit code: 2 if anything needs attention (rejects), else 0.
    return 2 if rejects else 0


if __name__ == "__main__":
    raise SystemExit(main())
