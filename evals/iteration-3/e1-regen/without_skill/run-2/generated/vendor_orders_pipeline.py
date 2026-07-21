#!/usr/bin/env python3
"""
Generated pipeline for spec `vendor_orders` (etlspec 0.1, taxonomy 0.1).

This file was regenerated FROM eval-inputs/vendor_orders.etlspec.yaml. Every
edge-case decision below is a compiled reflection of a recorded spec decision;
none were invented here. Taxonomy IDs from the spec are cited in comments.

Stdlib-only. Usage:
    python3 vendor_orders_pipeline.py <input.csv> [--outdir DIR]

Outputs (three report granularities, always emitted):
  <outdir>/vendor_orders_clean.csv      conformed rows
  <outdir>/vendor_orders_errors.jsonl   per-row error records (ERR-03)
  <outdir>/vendor_orders_run_report.json  per-error-type aggregates + run summary + manifest

Exit codes:
  0  success (no quarantine)
  2  completed with quarantine (within error budget)
  3  failed: error budget exceeded (run-error)
  4  failed: structural/config error (bad header, unreadable input)
"""

import argparse
import csv
import datetime as _dt
import hashlib
import json
import math
import os
import sys
import tempfile
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# --------------------------------------------------------------------------
# Compiled spec constants (source of truth: vendor_orders.etlspec.yaml)
# --------------------------------------------------------------------------
SPEC_NAME = "vendor_orders"
ETLSPEC_VERSION = "0.1"
TAXONOMY_VERSION = "0.1"

SOURCE_ENCODING = "utf-8"          # source.encoding (detected-confirmed)
SOURCE_DELIMITER = ","             # source.dialect.delimiter
SOURCE_QUOTECHAR = '"'             # source.dialect.quotechar
EXPECTED_COLUMNS = ["order_id", "cust_name", "amt", "dt", "active", "zip"]

# policies
POLICY = {
    "unicode_normalization": "NFC",          # default
    "strip_control_chars": True,             # default
    "normalize_unicode_whitespace": True,    # default
    "trim_whitespace": True,                 # default
    "empty_string_is_null": True,            # explicit
    "null_propagation": "sql",               # default -> None flows through transforms
    "datetime_rendering": "iso8601",         # default
    "error_disposition": "quarantine",       # default
    "error_budget": {"percent": 25, "min_rows": 2},  # explicit
    "duplicate_rows": "keep",                # default -> no dedup
}

# order_date sentinels (NUL-03 sentinel handling, detected-confirmed)
DATE_SENTINELS = {"N/A"}
DATE_FORMATS = ["%m/%d/%Y"]                  # TYP-03 MDY (explicit)


# --------------------------------------------------------------------------
# Warning / auto-fix tally (ERR-04: every auto-fix is counted, per column,
# per taxonomy ID; warnings never quarantine).
# --------------------------------------------------------------------------
class Tally:
    def __init__(self):
        self._counts = {}   # (column, code) -> count
        self._labels = {}   # code -> human label

    def bump(self, column, code, label):
        key = (column, code)
        self._counts[key] = self._counts.get(key, 0) + 1
        self._labels[code] = label

    def as_records(self):
        out = []
        for (column, code), n in sorted(self._counts.items()):
            out.append({"column": column, "code": code,
                        "label": self._labels[code], "count": n})
        return out

    def total(self):
        return sum(self._counts.values())


# --------------------------------------------------------------------------
# Row-level error, raised to trigger quarantine of the whole row.
# --------------------------------------------------------------------------
class RowError(Exception):
    def __init__(self, code, column, message):
        super().__init__(message)
        self.code = code
        self.column = column
        self.message = message


# --------------------------------------------------------------------------
# String hygiene, applied to every raw cell before any type transform.
# Order: NFC -> strip control chars -> normalize unicode whitespace ->
# trim -> empty-string-is-null. (ENC/STR/NUL policy chain.)
# Returns (value_or_None, list_of_(code,label) fixes applied).
# --------------------------------------------------------------------------
def clean_cell(raw):
    fixes = []
    s = raw if raw is not None else ""

    if POLICY["unicode_normalization"]:
        norm = unicodedata.normalize(POLICY["unicode_normalization"], s)
        if norm != s:
            fixes.append(("ENC-06", "unicode normalization (NFC)"))
        s = norm

    if POLICY["strip_control_chars"]:
        # drop Cc control chars (tab/newline included; these are cell values)
        stripped = "".join(ch for ch in s if unicodedata.category(ch) != "Cc")
        if stripped != s:
            fixes.append(("STR-05", "control characters removed"))
        s = stripped

    if POLICY["normalize_unicode_whitespace"]:
        # map any unicode whitespace (Zs, and common separators) to ASCII space
        conv = "".join(
            " " if (ch != " " and (ch.isspace() or unicodedata.category(ch) == "Zs")) else ch
            for ch in s
        )
        if conv != s:
            fixes.append(("STR-06", "unicode whitespace normalized"))
        s = conv

    if POLICY["trim_whitespace"]:
        trimmed = s.strip()
        if trimmed != s:
            fixes.append(("STR-04", "leading/trailing whitespace trimmed"))
        s = trimmed

    if POLICY["empty_string_is_null"] and s == "":
        return None, fixes   # NUL-01: empty string -> null

    return s, fixes


# --------------------------------------------------------------------------
# Transforms (one per mapping). Each takes the cleaned value (str or None)
# and the Tally, returns the conformed python value. None propagates (SQL
# null propagation) unless the target is non-nullable, which raises RowError.
# --------------------------------------------------------------------------
def t_string_required(value, column, tally):
    # order_id / customer_name: target nullable=false
    if value is None:
        raise RowError("NUL-04", column, "required (non-nullable) field is null/empty")
    return value


def t_amount(value, column, tally):
    # target amount: decimal scale 2, nullable
    # TYP-01: strip $ and commas; (n) = negative  (detected-confirmed)
    if value is None:
        return None
    s = value
    negative = False

    # accounting_negative: (n) => negative
    if len(s) >= 2 and s[0] == "(" and s[-1] == ")":
        s = s[1:-1].strip()
        negative = True
        tally.bump(column, "TYP-01", "accounting-negative parentheses -> minus")

    # currency: strip currency symbols (Sc) e.g. '$'
    cur = "".join(ch for ch in s if unicodedata.category(ch) != "Sc")
    if cur != s:
        tally.bump(column, "TYP-01", "currency symbol stripped")
    s = cur.strip()

    # thousands separator ","
    if "," in s:
        s = s.replace(",", "")
        tally.bump(column, "TYP-01", "thousands separator removed")

    # explicit leading sign
    if s.startswith("+"):
        s = s[1:]
    if s.startswith("-"):
        negative = True
        s = s[1:]

    if s == "":
        raise RowError("TYP-01", column, "empty numeric after currency/format stripping")

    try:
        dec = Decimal(s)
    except InvalidOperation:
        raise RowError("TYP-01", column, "not a valid decimal: %r" % value)

    if negative:
        dec = -dec
    dec = dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)  # scale 2
    return dec


def t_order_date(value, column, tally):
    # target order_date: date, nullable
    # TYP-03 MDY (%m/%d/%Y, explicit); NUL-03 sentinel "N/A" -> null
    if value is None:
        return None
    if value in DATE_SENTINELS:
        tally.bump(column, "NUL-03", "date sentinel -> null")
        return None
    for fmt in DATE_FORMATS:
        try:
            return _dt.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise RowError("TYP-03", column, "unparseable date (expected MDY %%m/%%d/%%Y): %r" % value)


def t_is_active(value, column, tally):
    # target is_active: boolean, nullable
    # TYP-06 Y/N vocabulary (detected-confirmed)
    if value is None:
        return None
    mapping = {"Y": True, "N": False}
    if value in mapping:
        return mapping[value]
    raise RowError("TYP-06", column, "outside Y/N boolean vocabulary: %r" % value)


def t_postal_code(value, column, tally):
    # target postal_code: string, nullable, max_length 10
    # TYP-07 keep as string (leading zeros preserved)
    if value is None:
        return None
    if len(value) > 10:
        # do not silently truncate -> quarantine (STR truncation risk)
        raise RowError("STR-07", column, "exceeds max_length 10: %r" % value)
    return value


# mapping order defines output column order
MAPPINGS = [
    ("order_id",      "order_id",  t_string_required),
    ("customer_name", "cust_name", t_string_required),
    ("amount",        "amt",       t_amount),
    ("order_date",    "dt",        t_order_date),
    ("is_active",     "active",    t_is_active),
    ("postal_code",   "zip",       t_postal_code),
]
TARGET_COLUMNS = [m[0] for m in MAPPINGS]


# --------------------------------------------------------------------------
# Output rendering
# --------------------------------------------------------------------------
def render(value):
    if value is None:
        return ""                      # null -> empty (empty_string_is_null symmetry)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, _dt.date):
        return value.isoformat()       # ISO-8601 date (datetime_rendering)
    if isinstance(value, Decimal):
        return format(value, "f")      # fixed-point, no exponent
    return str(value)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_text(path, text):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--outdir", default=".")
    args = ap.parse_args(argv)

    os.makedirs(args.outdir, exist_ok=True)
    clean_path = os.path.join(args.outdir, "vendor_orders_clean.csv")
    errors_path = os.path.join(args.outdir, "vendor_orders_errors.jsonl")
    report_path = os.path.join(args.outdir, "vendor_orders_run_report.json")

    tally = Tally()
    error_records = []          # per-row (ERR-03 granularity 1)
    clean_rows = []
    rows_in = 0

    started = _dt.datetime.now(_dt.timezone.utc).isoformat()

    try:
        with open(args.input, "r", encoding=SOURCE_ENCODING, newline="") as f:
            reader = csv.reader(f, delimiter=SOURCE_DELIMITER, quotechar=SOURCE_QUOTECHAR)
            try:
                header = next(reader)
            except StopIteration:
                sys.stderr.write("empty input file\n")
                return 4

            # header conformance (structural)
            header = [h.strip() for h in header]
            if header != EXPECTED_COLUMNS:
                sys.stderr.write(
                    "header mismatch:\n  expected %r\n  got      %r\n"
                    % (EXPECTED_COLUMNS, header))
                return 4

            for lineno, raw_row in enumerate(reader, start=2):
                # skip fully-empty lines silently
                if raw_row == [] or all(c.strip() == "" for c in raw_row):
                    continue
                rows_in += 1

                # ragged-row guard (structural)
                if len(raw_row) != len(EXPECTED_COLUMNS):
                    error_records.append({
                        "line": lineno, "code": "STR-01", "column": None,
                        "message": "ragged row: expected %d fields, got %d"
                                   % (len(EXPECTED_COLUMNS), len(raw_row)),
                        "raw": raw_row,
                    })
                    continue

                src = dict(zip(EXPECTED_COLUMNS, raw_row))

                # 1) clean every cell, tally hygiene fixes
                cleaned = {}
                for col in EXPECTED_COLUMNS:
                    val, fixes = clean_cell(src[col])
                    cleaned[col] = val
                    for code, label in fixes:
                        tally.bump(col, code, label)

                # 2) run transforms; first failure quarantines the row
                out = {}
                try:
                    for tgt, source_col, fn in MAPPINGS:
                        out[tgt] = fn(cleaned[source_col], tgt, tally)
                except RowError as e:
                    error_records.append({
                        "line": lineno, "code": e.code, "column": e.column,
                        "message": e.message, "raw": raw_row,
                    })
                    continue

                clean_rows.append([render(out[c]) for c in TARGET_COLUMNS])

    except FileNotFoundError:
        sys.stderr.write("input not found: %s\n" % args.input)
        return 4
    except UnicodeDecodeError as e:
        sys.stderr.write("encoding error (expected %s): %s\n" % (SOURCE_ENCODING, e))
        return 4

    # ---- error budget (percent OR min_rows, whichever is larger) ----
    budget = POLICY["error_budget"]
    quarantined = len(error_records)
    budget_limit = max(budget["min_rows"], math.ceil(budget["percent"] / 100.0 * rows_in))
    budget_exceeded = quarantined > budget_limit

    # ---- write clean output (atomic) ----
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(TARGET_COLUMNS)
    w.writerows(clean_rows)
    atomic_write_text(clean_path, buf.getvalue())

    # ---- per-row error records (ERR-03 granularity 1) ----
    atomic_write_text(errors_path,
                      "".join(json.dumps(r) + "\n" for r in error_records))

    # ---- aggregates by error type (ERR-03 granularity 2) ----
    agg = {}
    for r in error_records:
        agg[r["code"]] = agg.get(r["code"], 0) + 1

    finished = _dt.datetime.now(_dt.timezone.utc).isoformat()

    if budget_exceeded:
        status, exit_code = "failed_error_budget", 3
    elif quarantined > 0:
        status, exit_code = "completed_with_quarantine", 2
    else:
        status, exit_code = "success", 0

    # ---- run summary + manifest (ERR-03 granularity 3) ----
    report = {
        "spec": {"name": SPEC_NAME, "etlspec": ETLSPEC_VERSION,
                 "taxonomy": TAXONOMY_VERSION},
        "input": os.path.abspath(args.input),
        "started": started,
        "finished": finished,
        "status": status,
        "counts": {
            "rows_in": rows_in,
            "rows_out": len(clean_rows),
            "rows_quarantined": quarantined,
            "auto_fixes": tally.total(),
        },
        "error_budget": {
            "percent": budget["percent"], "min_rows": budget["min_rows"],
            "limit_rows": budget_limit, "exceeded": budget_exceeded,
        },
        "errors_by_type": agg,
        "auto_fixes_by_column_and_code": tally.as_records(),
        "policies": POLICY,
        "manifest": {
            "clean_output": {
                "path": os.path.abspath(clean_path),
                "rows": len(clean_rows),
                "sha256": sha256_of(clean_path),
            },
            "error_records": {
                "path": os.path.abspath(errors_path),
                "rows": quarantined,
                "sha256": sha256_of(errors_path),
            },
        },
    }
    atomic_write_text(report_path, json.dumps(report, indent=2) + "\n")

    # console summary
    sys.stderr.write(
        "vendor_orders: status=%s rows_in=%d rows_out=%d quarantined=%d "
        "(budget_limit=%d) auto_fixes=%d\n"
        % (status, rows_in, len(clean_rows), quarantined, budget_limit, tally.total()))
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
