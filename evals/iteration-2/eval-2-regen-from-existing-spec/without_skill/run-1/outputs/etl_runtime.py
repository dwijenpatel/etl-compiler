"""etl_runtime.py — shared, stdlib-only edge-case runtime for generated ETL pipelines.

This is the single place where edge-case semantics live. Generated pipelines are
thin orchestration that import these primitives. Every coded failure/warning uses a
taxonomy-style ID (ENC/STR/NUL/TYP/KEY/ERR) so error codes in output == taxonomy IDs.

Written from scratch for the vendor_orders regeneration task; stdlib only.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class TransformError(Exception):
    """A row-level, coded conversion failure. `code` is a taxonomy ID."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Fix accounting (ERR-04: auto-fixes are counted, per column per taxonomy ID)
# ---------------------------------------------------------------------------
class FixCounter:
    def __init__(self):
        # column -> {"TYP-01/strip_currency": n, ...}
        self.counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def add(self, column: str, fixes: list[str]):
        for f in fixes:
            self.counts[column][f] += 1

    def as_dict(self) -> dict:
        return {col: dict(d) for col, d in self.counts.items()}

    def total(self) -> int:
        return sum(n for d in self.counts.values() for n in d.values())


# ---------------------------------------------------------------------------
# Cell normalization (ENC-06 NFC, STR control + whitespace, NUL-01 empty->null)
# ---------------------------------------------------------------------------
# Non-ASCII unicode whitespace collapsed to a plain ASCII space (built from
# explicit code points to avoid ambiguity of literal characters in source).
_ZS_CODEPOINTS = [
    0x00A0, 0x1680,
    0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007,
    0x2008, 0x2009, 0x200A, 0x202F, 0x205F, 0x3000,
]
_ZS_WHITESPACE_RE = re.compile("[" + "".join(chr(c) for c in _ZS_CODEPOINTS) + "]")


def normalize_cell(raw, *, unicode_normalization="NFC", strip_control_chars=True,
                   normalize_unicode_whitespace=True, trim_whitespace=True,
                   empty_string_is_null=True):
    """Apply the source-level text policies. Returns (value_or_None, fixes:list[str])."""
    if raw is None:
        return None, []
    fixes: list[str] = []
    s = raw

    if unicode_normalization:
        n = unicodedata.normalize(unicode_normalization, s)
        if n != s:
            fixes.append(f"ENC-06/{unicode_normalization.lower()}")
        s = n

    if strip_control_chars:
        # drop Cc control characters (CSV cells shouldn't carry them)
        n = "".join(ch for ch in s if unicodedata.category(ch) != "Cc")
        if n != s:
            fixes.append("STR-05/strip_control_chars")
        s = n

    if normalize_unicode_whitespace:
        n = _ZS_WHITESPACE_RE.sub(" ", s)
        if n != s:
            fixes.append("STR-06/normalize_unicode_whitespace")
        s = n

    if trim_whitespace:
        n = s.strip()
        if n != s:
            fixes.append("STR-02/trim_whitespace")
        s = n

    if empty_string_is_null and s == "":
        return None, fixes

    return s, fixes


# ---------------------------------------------------------------------------
# Type transforms
# ---------------------------------------------------------------------------
_CURRENCY_RE = re.compile("[" + "".join(chr(c) for c in (0x24, 0xA3, 0x20AC, 0xA5)) + "]")


def to_decimal(raw, *, scale=2, thousands_sep=",", currency=False,
               accounting_negative=False):
    """TYP-01/TYP-02: parse a monetary/numeric string to a scaled Decimal.

    Returns (Decimal_or_None, fixes). Raises TransformError('TYP-01', ...) on failure.
    """
    if raw is None:
        return None, []
    fixes: list[str] = []
    s = raw.strip()
    negative = False

    if accounting_negative and s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
        fixes.append("TYP-01/accounting_negative")

    if s.startswith("-"):
        negative = True
        s = s[1:].strip()

    if currency:
        n = _CURRENCY_RE.sub("", s)
        if n != s:
            fixes.append("TYP-01/strip_currency")
        s = n

    if thousands_sep and thousands_sep in s:
        s = s.replace(thousands_sep, "")
        fixes.append("TYP-01/strip_thousands_sep")

    s = s.strip()
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        raise TransformError("TYP-01", f"cannot parse decimal from {raw!r}")

    if negative:
        d = -d
    quantum = Decimal(1).scaleb(-scale)  # scale=2 -> Decimal('0.01')
    d = d.quantize(quantum, rounding=ROUND_HALF_UP)
    return d, fixes


def to_date(raw, *, formats, sentinels=()):
    """TYP-03: parse a date under an explicit, ordered set of formats.

    Sentinels (NUL-03) map to None. Returns (date_or_None, fixes).
    Raises TransformError('TYP-03', ...) on failure.
    """
    if raw is None:
        return None, []
    if raw in sentinels:
        return None, ["NUL-03/sentinel_to_null"]
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date(), []
        except ValueError:
            continue
    raise TransformError("TYP-03", f"cannot parse date from {raw!r} using {list(formats)}")


def to_bool(raw, *, mapping):
    """TYP-06: map a controlled boolean vocabulary. Returns (bool_or_None, fixes)."""
    if raw is None:
        return None, []
    if raw in mapping:
        return mapping[raw], []
    raise TransformError("TYP-06", f"unrecognized boolean token {raw!r}; expected one of {sorted(mapping)}")


# ---------------------------------------------------------------------------
# Constraint enforcement
# ---------------------------------------------------------------------------
def enforce_string(value, *, nullable, max_length=None, column):
    """String column constraints. Never silently truncates (STR-07)."""
    if value is None:
        if not nullable:
            raise TransformError("NUL-05", f"non-nullable column {column!r} is null")
        return None
    if max_length is not None and len(value) > max_length:
        raise TransformError("STR-07", f"value in {column!r} exceeds max_length {max_length}: {value!r}")
    return value


def enforce_not_null(value, *, nullable, column):
    if value is None and not nullable:
        raise TransformError("NUL-05", f"non-nullable column {column!r} is null")
    return value


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------
def render(value) -> str:
    """Render a converted value for CSV output. None -> empty string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):  # date -> ISO-8601
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Error budget (ERR-01)
# ---------------------------------------------------------------------------
def budget_threshold(total_rows: int, *, percent: float, min_rows: int) -> int:
    """Max tolerated quarantined rows: max(min_rows, percent-of-total)."""
    pct_allow = (percent / 100.0) * total_rows
    return int(max(min_rows, pct_allow))
