"""etl_runtime.py — shared edge-case semantics for generated ETL pipelines.

Stdlib-only. This is the ONE place edge-case behavior is implemented; generated
pipelines are thin orchestration that import these helpers. Every coded outcome
(auto-fix tally or quarantine reason) carries a taxonomy ID so reports and error
records speak a stable vocabulary.

Taxonomy IDs referenced here:
  ENC-*  encoding damage
  STR-*  string hygiene (control chars, unicode whitespace, trim)
  NUL-*  nulls / sentinels
  TYP-01 numeric/decimal parsing (currency, thousands sep, accounting negatives)
  TYP-03 date field ordering (MDY / DMY)
  TYP-06 boolean vocabulary
  TYP-07 numeric-looking value kept as string
  ERR-03 three-granularity reporting   ERR-04 auto-fixes are counted
"""

import csv
import hashlib
import json
import os
import tempfile
import unicodedata
from datetime import datetime, date, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


# --------------------------------------------------------------------------- #
# Error / result carriers
# --------------------------------------------------------------------------- #

class RowError(Exception):
    """A recoverable, row-scoped failure. Carries a taxonomy ID so the reject
    record is coded, not free-text."""

    def __init__(self, tax_id, message, column=None):
        super().__init__(message)
        self.tax_id = tax_id
        self.message = message
        self.column = column


class FieldResult:
    """Value produced for one target field, plus the taxonomy IDs of any
    auto-fixes applied while producing it (each fix is tallied per ERR-04)."""

    __slots__ = ("value", "fixes")

    def __init__(self, value, fixes=None):
        self.value = value
        self.fixes = fixes or []


# --------------------------------------------------------------------------- #
# String hygiene policies (applied to every raw cell before typing)
# --------------------------------------------------------------------------- #

def _is_control(ch):
    # Cc = control, Cf = format (zero-width joiners, BOM, bidi marks, ...)
    return unicodedata.category(ch) in ("Cc", "Cf")


def apply_string_policies(raw, policies):
    """Run the configured string-hygiene policies over one raw cell.

    Returns a FieldResult whose value is the cleaned string or None (when
    empty_string_is_null fires). Order is deliberate: normalize encoding first,
    then strip damage, then collapse whitespace, then trim, then null-check.
    """
    fixes = []
    if raw is None:
        return FieldResult(None, fixes)
    s = raw
    original = s

    # ENC / STR-05: canonicalize composed form so equal text compares equal.
    if policies.get("unicode_normalization"):
        s = unicodedata.normalize(policies["unicode_normalization"], s)

    # STR-06: drop control/format characters (tabs/newlines inside a quoted
    # field included — they are damage in a flat feed).
    if policies.get("strip_control_chars"):
        stripped = "".join(ch for ch in s if not _is_control(ch))
        if stripped != s:
            fixes.append("STR-06")
        s = stripped

    # STR-07: fold non-ASCII whitespace (NBSP, thin space, ...) to plain space.
    if policies.get("normalize_unicode_whitespace"):
        folded = "".join(" " if (ch != " " and ch.isspace()) else ch for ch in s)
        if folded != s:
            fixes.append("STR-07")
        s = folded

    # STR-02: strip leading/trailing whitespace.
    if policies.get("trim_whitespace"):
        trimmed = s.strip()
        if trimmed != s:
            fixes.append("STR-02")
        s = trimmed

    # NUL-01: empty string becomes NULL when the policy says so.
    if policies.get("empty_string_is_null") and s == "":
        return FieldResult(None, fixes)

    # De-dup fix codes but keep at most-one-per-code semantics for this cell.
    if original != s:
        fixes = list(dict.fromkeys(fixes))
    return FieldResult(s, fixes)


# --------------------------------------------------------------------------- #
# Sentinel handling (NUL-03)
# --------------------------------------------------------------------------- #

def apply_sentinels(value, sentinel_values):
    """If value matches a configured sentinel token, return (None, ['NUL-03']).
    Otherwise pass through unchanged."""
    if value is not None and sentinel_values and value in sentinel_values:
        return FieldResult(None, ["NUL-03"])
    return FieldResult(value, [])


# --------------------------------------------------------------------------- #
# Type coercions
# --------------------------------------------------------------------------- #

def to_decimal(value, scale=2, thousands_sep=",", currency=False,
               accounting_negative=False):
    """Parse a money-ish string to Decimal quantized to `scale`.

    Honors, in order: accounting negatives ``(n)`` -> -n, currency symbol
    stripping, thousands-separator removal. Every clean-up that actually changed
    the token is reported as a TYP-01 fix. Returns FieldResult(None) for a null
    input (SQL null propagation). Raises RowError(TYP-01) if unparseable.
    """
    if value is None:
        return FieldResult(None, [])
    s = value.strip()
    if s == "":
        return FieldResult(None, [])

    fixes = []
    original = s
    negative = False

    # Accounting negative: (1,234.50) -> -1234.50
    if accounting_negative and s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
        negative = True

    # Leading/trailing currency symbols (any Unicode currency-symbol char, plus $).
    if currency:
        s2 = s.strip()
        s2 = s2.lstrip("$").rstrip("$")
        s2 = "".join(ch for ch in s2 if unicodedata.category(ch) != "Sc")
        s2 = s2.strip()
        s = s2

    # Thousands separators.
    if thousands_sep:
        s = s.replace(thousands_sep, "")

    # A bare leading +/- sign is still allowed after clean-up.
    s = s.strip()

    if s != original:
        fixes.append("TYP-01")

    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        raise RowError("TYP-01", f"cannot parse decimal from {value!r}")

    if negative:
        d = -d

    quant = Decimal(1).scaleb(-scale) if scale else Decimal(1)
    d = d.quantize(quant, rounding=ROUND_HALF_UP)
    return FieldResult(d, fixes)


def to_date(value, formats, sentinels=None):
    """Parse a date using the first matching format in `formats`.

    `formats` are explicit (e.g. ['%m/%d/%Y']) so field ordering is decided by
    the spec (TYP-03), never guessed. Sentinel tokens map to NULL (NUL-03).
    Raises RowError(TYP-03) when no format matches.
    """
    if value is None:
        return FieldResult(None, [])
    sres = apply_sentinels(value, sentinels or [])
    if sres.value is None:
        return FieldResult(None, sres.fixes)
    s = sres.value.strip()
    if s == "":
        return FieldResult(None, [])
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            return FieldResult(dt.date(), [])
        except ValueError:
            continue
    raise RowError("TYP-03", f"cannot parse date from {value!r} using {formats}")


def to_bool(value, mapping):
    """Map a token to a bool using an explicit vocabulary (TYP-06).

    Tries an exact match, then a case-folded match, so 'y'/'Y' both resolve.
    Raises RowError(TYP-06) for tokens outside the vocabulary."""
    if value is None:
        return FieldResult(None, [])
    s = value.strip()
    if s == "":
        return FieldResult(None, [])
    if s in mapping:
        return FieldResult(bool(mapping[s]), [])
    lowered = {k.lower(): v for k, v in mapping.items()}
    if s.lower() in lowered:
        # Case normalization counts as a boolean-vocabulary fix.
        return FieldResult(bool(lowered[s.lower()]), ["TYP-06"])
    raise RowError("TYP-06", f"value {value!r} not in boolean vocabulary "
                             f"{sorted(mapping)}")


def as_string(value, max_length=None):
    """Identity for strings (TYP-07 keeps numeric-looking IDs/zips as text).

    Enforces max_length by *rejecting* over-length values rather than silently
    truncating — silent truncation is itself a taxonomy failure and there is no
    recorded truncate decision in the spec."""
    if value is None:
        return FieldResult(None, [])
    if max_length is not None and len(value) > max_length:
        raise RowError("STR-04", f"value length {len(value)} exceeds "
                                 f"max_length {max_length}: {value!r}")
    return FieldResult(value, [])


# --------------------------------------------------------------------------- #
# Nullability
# --------------------------------------------------------------------------- #

def check_nullable(value, column, nullable):
    if value is None and not nullable:
        raise RowError("NUL-02", f"NULL in non-nullable column {column!r}",
                       column=column)
    return value


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render_cell(value, datetime_rendering="iso8601"):
    """Render a typed value to its output-string form for CSV."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


# --------------------------------------------------------------------------- #
# Run bookkeeping (ERR-03 three-granularity reports, ERR-04 fix counting)
# --------------------------------------------------------------------------- #

class RunReport:
    def __init__(self, spec_name, taxonomy_version, source_path):
        self.spec_name = spec_name
        self.taxonomy_version = taxonomy_version
        self.source_path = source_path
        self.rows_read = 0
        self.rows_written = 0
        self.rows_quarantined = 0
        # ERR-04: per-column, per-taxonomy fix tallies.
        self.fixes = {}          # {column: {tax_id: count}}
        # ERR-03 mid-granularity: per-taxonomy quarantine aggregates.
        self.error_types = {}    # {tax_id: count}
        # ERR-03 fine-granularity: one record per quarantined row.
        self.reject_records = []
        self.started_at = datetime.now(timezone.utc).isoformat()

    def tally_fix(self, column, tax_id):
        self.fixes.setdefault(column, {})
        self.fixes[column][tax_id] = self.fixes[column].get(tax_id, 0) + 1

    def tally_fixes(self, column, tax_ids):
        for t in tax_ids:
            self.tally_fix(column, t)

    def record_reject(self, row_number, raw_row, err):
        self.rows_quarantined += 1
        self.error_types[err.tax_id] = self.error_types.get(err.tax_id, 0) + 1
        self.reject_records.append({
            "row_number": row_number,
            "tax_id": err.tax_id,
            "column": err.column,
            "reason": err.message,
            "raw": raw_row,
        })

    def total_fixes(self):
        return sum(c for col in self.fixes.values() for c in col.values())

    def as_manifest(self, source_sha256, error_budget, budget_exceeded,
                    exit_status, output_path, reject_path):
        return {
            "spec_name": self.spec_name,
            "taxonomy_version": self.taxonomy_version,
            "source": {"path": self.source_path, "sha256": source_sha256},
            "started_at": self.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "rows_read": self.rows_read,
            "rows_written": self.rows_written,
            "rows_quarantined": self.rows_quarantined,
            "auto_fixes_total": self.total_fixes(),
            "auto_fixes_by_column": self.fixes,
            "error_types": self.error_types,
            "error_budget": {
                "config": error_budget,
                "exceeded": budget_exceeded,
            },
            "outputs": {"clean": output_path, "rejects": reject_path},
            "exit_status": exit_status,
        }


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_csv(path, header, rows):
    """Write rows atomically: temp file in the same dir, then os.replace."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, default=str)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def evaluate_budget(rows_read, rows_quarantined, error_budget):
    """Budget is exceeded only when quarantines exceed BOTH the percentage
    allowance AND the min_rows floor (the floor protects tiny files)."""
    pct = error_budget.get("percent", 0)
    min_rows = error_budget.get("min_rows", 0)
    allowance = max(min_rows, (pct / 100.0) * rows_read)
    return rows_quarantined > allowance, allowance
