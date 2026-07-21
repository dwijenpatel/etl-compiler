"""etl_runtime.py — self-contained ETL edge-case runtime (stdlib-only).

Written from scratch for this task. Implements the string-cleaning and typed
conversion semantics that a mapping spec references, plus row-level error
quarantine and auto-fix accounting.

Error codes used in quarantine/warning records:
  TYP-01  decimal parse failure (amount)
  TYP-03  date parse failure (order_date)
  TYP-06  boolean vocabulary miss (is_active)
  NUL-CONSTRAINT   null produced for a non-nullable target column
  STR-MAXLEN       value longer than the target column's max_length
  ROW-RAGGED       source row field count != header field count

Fix codes (warnings, always counted, never quarantine):
  FIX-CLEAN   string cleaning changed the raw value
  FIX-CUR     currency symbol / thousands separators removed
  FIX-NEG     accounting-style negative "(n)" applied
  FIX-NULL    recognized sentinel mapped to null
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


# --- NULL sentinel -----------------------------------------------------------
class _Null:
    """A distinct null marker so we never confuse 'absent' with the string ''."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "NULL"

    def __bool__(self):
        return False


NULL = _Null()


# --- Row-level error --------------------------------------------------------
class CellError(Exception):
    """Raised by a transform when a cell cannot be produced; triggers quarantine."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class FixLog:
    """Per-(column, code) tally of auto-fixes applied (ERR-04-style accounting)."""

    counts: dict = field(default_factory=dict)

    def add(self, column: str, code: str):
        self.counts[(column, code)] = self.counts.get((column, code), 0) + 1

    def as_list(self):
        return [
            {"column": col, "code": code, "count": n}
            for (col, code), n in sorted(self.counts.items())
        ]


# --- String cleaning --------------------------------------------------------
_UNICODE_WS_EXTRA = {"", " ", " "}


def clean_string(raw, column, policies, fixlog):
    """Apply the source-level string policies, in a fixed order.

    Returns NULL if empty_string_is_null and the cleaned value is ''.
    Records FIX-CLEAN if any change was made.
    """
    if raw is None:
        return NULL
    s = raw

    if policies.get("unicode_normalization"):
        s = unicodedata.normalize(policies["unicode_normalization"], s)

    if policies.get("strip_control_chars"):
        # Remove Unicode control (Cc) and format (Cf) characters.
        s = "".join(
            ch for ch in s if unicodedata.category(ch) not in ("Cc", "Cf")
        )

    if policies.get("normalize_unicode_whitespace"):
        s = "".join(
            " " if (unicodedata.category(ch) == "Zs" or ch in _UNICODE_WS_EXTRA)
            else ch
            for ch in s
        )

    if policies.get("trim_whitespace"):
        s = s.strip()

    if s != raw:
        fixlog.add(column, "FIX-CLEAN")

    if policies.get("empty_string_is_null") and s == "":
        return NULL
    return s


# --- Typed conversions ------------------------------------------------------
def to_string(value, column, *, max_length=None, **_):
    if value is NULL:
        return NULL
    if max_length is not None and len(value) > max_length:
        raise CellError(
            "STR-MAXLEN",
            f"value length {len(value)} exceeds max_length {max_length}",
        )
    return value


def to_decimal(value, column, fixlog, *, thousands_sep=None, currency=False,
               accounting_negative=False, scale=None, **_):
    if value is NULL:
        return NULL
    s = value
    negative = False

    if accounting_negative and s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
        negative = True
        fixlog.add(column, "FIX-NEG")

    changed_cur = False
    if currency:
        # Strip common currency symbols.
        for sym in ("$", "€", "£", "¥", "USD", "usd"):
            if sym in s:
                s = s.replace(sym, "")
                changed_cur = True
    if thousands_sep and thousands_sep in s:
        s = s.replace(thousands_sep, "")
        changed_cur = True
    s = s.strip()
    if changed_cur:
        fixlog.add(column, "FIX-CUR")

    if s.startswith("-"):
        negative = True
        s = s[1:].strip()

    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        raise CellError("TYP-01", f"cannot parse decimal from {value!r}")
    if negative:
        d = -d
    if scale is not None:
        q = Decimal(1).scaleb(-scale)  # 10**-scale
        d = d.quantize(q, rounding=ROUND_HALF_UP)
    return d


def to_date(value, column, fixlog, *, formats, sentinels=(), **_):
    from datetime import datetime

    if value is NULL:
        return NULL
    if value in sentinels:
        fixlog.add(column, "FIX-NULL")
        return NULL
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise CellError(
        "TYP-03", f"cannot parse date from {value!r} using formats {formats}"
    )


def to_bool(value, column, *, mapping, **_):
    if value is NULL:
        return NULL
    if value in mapping:
        return mapping[value]
    raise CellError(
        "TYP-06",
        f"value {value!r} not in boolean vocabulary {sorted(mapping)}",
    )


# --- Rendering for output ---------------------------------------------------
def render(value):
    """Serialize a typed cell to its output-CSV string form."""
    from datetime import date

    if value is NULL:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()  # iso8601
    return str(value)
