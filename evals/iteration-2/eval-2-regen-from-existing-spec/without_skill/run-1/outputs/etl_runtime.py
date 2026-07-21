"""etl_runtime.py — self-contained edge-case runtime for the vendor_orders pipeline.

Written from scratch for this task. Stdlib-only. Every edge-case semantic lives here so
the generated pipeline stays thin orchestration. Error/warning codes are taxonomy IDs.

Taxonomy IDs referenced:
  TYP-01  numeric/currency formatting (strip $, thousands sep, accounting negative)
  TYP-03  ambiguous date order (resolved to MDY per spec)
  TYP-06  boolean vocabulary (Y/N)
  TYP-07  type ambiguity resolved to string (preserve leading zeros)
  NUL-03  sentinel values -> null
  ERR-04  auto-fixes are counted, never silent
"""

import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


class CellError(Exception):
    """Raised when a cell cannot be coerced. `code` is a taxonomy ID."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# String hygiene policies (applied to every raw string cell before transforms)
# ---------------------------------------------------------------------------

_UNICODE_WS = {
    " ", " ", " ", " ", " ", " ", " ",
    " ", " ", " ", " ", " ", " ", " ",
    " ", " ", " ", "　",
}


def apply_string_policies(raw, policies, warn):
    """Apply source-wide string hygiene. Returns the cleaned string (or None if the
    cell becomes empty and empty_string_is_null is on). `warn(code)` tallies a fix."""
    if raw is None:
        return None
    s = raw

    if policies.get("strip_control_chars"):
        cleaned = "".join(
            ch for ch in s
            if ch in ("\t", "\n", "\r") or unicodedata.category(ch)[0] != "C"
        )
        if cleaned != s:
            warn("STR-05")  # control chars stripped
            s = cleaned

    if policies.get("normalize_unicode_whitespace"):
        cleaned = "".join(" " if ch in _UNICODE_WS else ch for ch in s)
        if cleaned != s:
            warn("STR-06")  # exotic unicode whitespace normalized
            s = cleaned

    if policies.get("unicode_normalization"):
        form = policies["unicode_normalization"]
        cleaned = unicodedata.normalize(form, s)
        if cleaned != s:
            warn("STR-04")  # unicode normalized
            s = cleaned

    if policies.get("trim_whitespace"):
        cleaned = s.strip()
        if cleaned != s:
            warn("STR-02")  # leading/trailing whitespace trimmed
            s = cleaned

    if policies.get("empty_string_is_null") and s == "":
        return None

    return s


# ---------------------------------------------------------------------------
# Type coercions
# ---------------------------------------------------------------------------

def to_string(value, max_length=None):
    """Identity string coercion (TYP-07). Enforces max_length as a hard error rather
    than silently truncating (STR-07 house rule: never silently truncate)."""
    if value is None:
        return None
    if max_length is not None and len(value) > max_length:
        raise CellError("STR-07", f"value length {len(value)} exceeds max_length {max_length}")
    return value


def to_decimal(value, scale, thousands_sep=None, currency=False,
               accounting_negative=False, warn=None):
    """Parse a decimal (TYP-01). Strips currency symbols and thousands separators;
    treats (n) as negative when accounting_negative is set. Quantizes to `scale`."""
    if value is None:
        return None
    s = value
    negative = False

    if accounting_negative and s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
        if warn:
            warn("TYP-01")  # accounting-style negative

    if currency:
        stripped = s.lstrip("$").strip()
        # also tolerate a trailing symbol
        stripped = stripped.rstrip("$").strip()
        if stripped != s:
            if warn:
                warn("TYP-01")  # currency symbol stripped
            s = stripped

    if thousands_sep:
        if thousands_sep in s:
            s = s.replace(thousands_sep, "")
            if warn:
                warn("TYP-01")  # thousands separator removed

    if s.startswith("-"):
        negative = True
        s = s[1:].strip()

    try:
        d = Decimal(s)
    except InvalidOperation:
        raise CellError("TYP-01", f"cannot parse decimal from {value!r}")

    if negative:
        d = -d

    quant = Decimal(1).scaleb(-scale)  # e.g. scale=2 -> Decimal('0.01')
    return d.quantize(quant, rounding=ROUND_HALF_UP)


def to_date(value, formats, rendering="iso8601"):
    """Parse a date under an explicit, finite list of formats (TYP-03 resolved upstream).
    Renders per `rendering`. Raises on no-match rather than guessing."""
    if value is None:
        return None
    from datetime import datetime
    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if rendering == "iso8601":
            return dt.date().isoformat()
        return dt.date().isoformat()
    raise CellError("TYP-03", f"date {value!r} matched none of {formats}")


def to_bool(value, mapping):
    """Map a controlled boolean vocabulary (TYP-06). Unknown token -> error."""
    if value is None:
        return None
    if value in mapping:
        return mapping[value]
    raise CellError("TYP-06", f"unrecognized boolean token {value!r}; expected one of {sorted(mapping)}")


def apply_sentinels(value, sentinel_values, warn=None):
    """Replace configured sentinel tokens with null (NUL-03)."""
    if value is None:
        return None
    if value in sentinel_values:
        if warn:
            warn("NUL-03")  # sentinel -> null
        return None
    return value
