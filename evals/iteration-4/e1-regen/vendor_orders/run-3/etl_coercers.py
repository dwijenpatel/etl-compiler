"""etl_coercers — the deterministic core of the etl-solved runtime.

Everything in this module is **deterministic and effect-confined**: same inputs
produce same outputs, and the only permitted effect is counting into the
explicitly-passed `RunReport` accumulator (ERR-04: every auto-fix is counted,
never silent). No I/O, no clock, no environment access lives here — that is
the driver's job (`etl_runtime.py`).

Every error/warning code refers to an entry in the ETL Failure-Mode Taxonomy
(references/taxonomy.md in the skill). Stdlib only — no third-party deps.

Ships together with `etl_runtime.py`; generated pipelines import only
`etl_runtime`, which re-exports this module's public API.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal, Mapping, Sequence, TypedDict, TypeVar, cast

TAXONOMY_VERSION = "0.4"
ISO8601_DATE = "%Y-%m-%d"
ISO8601_DATETIME = "%Y-%m-%dT%H:%M:%S%z"

T = TypeVar("T")

# The value vocabulary that flows between transforms in a pipeline: raw CSV
# fields arrive as str|None; coercers produce the typed variants.
CellValue = str | int | bool | Decimal | None


# ---------------------------------------------------------------------------
# Record shapes (the machine-readable report contract, ERR-03)
# ---------------------------------------------------------------------------

class RowErrorRecord(TypedDict):
    """Per-row error record (ERR-03 i). Field set adopts DuckDB's reject_errors
    design: stable code + column index + verbatim raw line, for reprocessing."""
    row_number: int
    column: str
    column_idx: int | None
    error_code: str
    offending_value: str | None
    message: str
    csv_line: str


class DuplicateRecord(TypedDict):
    """STR-05 exact-duplicate sighting (input-level accounting)."""
    row_number: int
    first_seen_row: int


class ChangeRecord(TypedDict):
    """One repaired field under the annotate disposition (ERR-01 c). Shape
    adopted from Airbyte's `_airbyte_meta.changes`; reasons are taxonomy IDs."""
    field: str
    change: Literal["NULLED"]
    reason: str


class AnnotationRecord(TypedDict):
    """Per-row change ledger entry (ERR-01 c)."""
    row_number: int
    changes: list[ChangeRecord]


class RunErrorInfo(TypedDict):
    """Run-level failure descriptor recorded in summary/manifest (ERR-05)."""
    code: str
    message: str


class SummaryDict(TypedDict):
    """The run summary (ERR-03 iii)."""
    rows_in: int
    rows_out: int
    rows_quarantined: int
    distinct_error_types: int
    errors_by_type: dict[str, int]
    warnings_by_type: dict[str, int]
    rows_annotated: int
    annotations_by_type: dict[str, int]
    exact_duplicate_rows: list[DuplicateRecord]
    run_error: RunErrorInfo | None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class RowError(Exception):
    """A row-level failure. code is a taxonomy ID (e.g. 'TYP-03')."""

    def __init__(self, code: str, column: str, value: object, message: str):
        self.code = code
        self.column = column
        self.value = value
        self.message = message
        super().__init__(f"{code} [{column}] {message} (value={value!r})")


class RunError(Exception):
    """A run-level failure. The run aborts; no partial output is written (ERR-05)."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class SkipRow(Exception):
    """Raise from transform_row to exclude a row per an explicit spec decision
    (e.g. STR-06 confirmed footer/preamble rows). Counted as a warning, never silent."""

    def __init__(self, code: str = "STR-06", reason: str = ""):
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


# ---------------------------------------------------------------------------
# Report accumulator (ERR-03 / ERR-04 / ERR-06)
# ---------------------------------------------------------------------------

@dataclass
class RunReport:
    """The one sanctioned effect target: coercers count fixes into this
    explicitly-passed accumulator; nothing else in the core mutates state."""

    rows_in: int = 0
    rows_out: int = 0
    row_errors: list[RowErrorRecord] = field(default_factory=list)
    quarantined: list[tuple[int, list[str]]] = field(default_factory=list)
    warnings: "Counter[tuple[str, str]]" = field(default_factory=Counter)
    duplicates: list[DuplicateRecord] = field(default_factory=list)
    annotations: list[AnnotationRecord] = field(default_factory=list)
    run_error: RunErrorInfo | None = None

    def warn(self, column: str | None, code: str, n: int = 1) -> None:
        """ERR-04: every auto-fix is counted, never silent."""
        if n:
            self.warnings[(column or "<row>", code)] += n

    def add_row_error(self, row_number: int, raw_row: list[str], err: RowError,
                      expected_columns: Sequence[str] | None = None) -> None:
        column_idx: int | None = None
        if expected_columns and err.column in expected_columns:
            column_idx = list(expected_columns).index(err.column)
        buf = io.StringIO()
        csv.writer(buf).writerow(raw_row)
        self.row_errors.append({
            "row_number": row_number,
            "column": err.column,
            "column_idx": column_idx,
            "error_code": err.code,
            "offending_value": None if err.value is None else str(err.value)[:500],
            "message": err.message,
            "csv_line": buf.getvalue().rstrip("\r\n"),
        })
        self.quarantined.append((row_number, raw_row))

    def annotate_row(self, row_number: int, changes: list[ChangeRecord]) -> None:
        """ERR-01 option (c): ledger for a row that loaded with repaired fields."""
        self.annotations.append({"row_number": row_number, "changes": changes})

    def annotation_aggregates(self) -> dict[str, int]:
        agg: Counter[tuple[str, str]] = Counter()
        for a in self.annotations:
            for c in a["changes"]:
                agg[(c["reason"], c["field"])] += 1
        return {f"{code}:{col}": n for (code, col), n in sorted(agg.items())}

    # -- aggregates (ERR-03 ii) --
    def error_aggregates(self) -> dict[str, int]:
        agg: Counter[tuple[str, str]] = Counter()
        for e in self.row_errors:
            agg[(e["error_code"], e["column"])] += 1
        return {f"{code}:{col}": n for (code, col), n in sorted(agg.items())}

    def warning_aggregates(self) -> dict[str, int]:
        return {f"{code}:{col}": n
                for (col, code), n in sorted(self.warnings.items(), key=lambda kv: kv[0][1])}

    def summary(self) -> SummaryDict:
        errors = self.error_aggregates()
        return {
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "rows_quarantined": len(self.quarantined),
            "distinct_error_types": len(errors),
            "errors_by_type": errors,
            "warnings_by_type": self.warning_aggregates(),
            "rows_annotated": len(self.annotations),
            "annotations_by_type": self.annotation_aggregates(),
            "exact_duplicate_rows": self.duplicates,
            "run_error": self.run_error,
        }


# ---------------------------------------------------------------------------
# Text cleaning (ENC-03 / ENC-04 / ENC-05) and null resolution (NUL-01/02/03)
# ---------------------------------------------------------------------------

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Unicode whitespace variants -> ASCII space; zero-width & BOM chars -> removed. (ENC-05)
_UNICODE_SPACE_RE = re.compile(
    "[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]"
)
_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")

_NormForm = Literal["NFC", "NFD", "NFKC", "NFKD"]


def clean_text(value: str | None, column: str, report: RunReport | None,
               *, normalization: str = "NFC", strip_control: bool = True,
               normalize_ws: bool = True) -> str | None:
    """Apply ENC-class fixes per policy. Counts every change as a warning."""
    if value is None:
        return None
    v = value
    if normalization:  # ENC-03
        # The spec loader constrains the value; an invalid form raises here,
        # surfacing as an ERR-05 run error (fail loud, never silently skipped).
        nv = unicodedata.normalize(cast(_NormForm, normalization), v)
        if nv != v and report:
            report.warn(column, "ENC-03")
        v = nv
    if strip_control:  # ENC-04
        nv = _CONTROL_CHARS_RE.sub("", v)
        if nv != v and report:
            report.warn(column, "ENC-04")
        v = nv
    if normalize_ws:  # ENC-05
        nv = _UNICODE_SPACE_RE.sub(" ", v)
        nv = _ZERO_WIDTH_RE.sub("", nv)
        if nv != v and report:
            report.warn(column, "ENC-05")
        v = nv
    return v


def resolve_null(value: str | None, column: str, report: RunReport | None,
                 *, trim: bool = True, empty_is_null: bool = True,
                 sentinels: tuple[str, ...] = ()) -> str | None:
    """NUL-01/02/03 resolution. Returns None or the (possibly trimmed) string."""
    if value is None:
        return None
    v = value
    if trim:
        t = v.strip()
        if t != v:
            if report:
                # ERR-04: every trim is counted — NUL-02 when the value was
                # whitespace-only, TYP-10 when content survived the trim.
                report.warn(column, "NUL-02" if t == "" else "TYP-10")
            v = t
    if sentinels and v in sentinels:
        if report:
            report.warn(column, "NUL-03")
        return None
    if empty_is_null and v == "":
        return None
    return v


# ---------------------------------------------------------------------------
# Coercers (TYP-*). All pass None through. All raise RowError on failure.
# ---------------------------------------------------------------------------

_PAREN_NEG_RE = re.compile(r"^\((.*)\)$")
_CURRENCY_RE = re.compile(r"^[\s]*[$€£¥₹]")
# TYP-12: magnitude/scale suffixes (10.00K, 1.2M). Applied only when the spec confirms it.
_MAGNITUDE = {"k": 3, "m": 6, "b": 9, "g": 9, "t": 12}


def _clean_numeric_string(v: str, *, thousands_sep: str | None, currency: bool,
                          accounting_negative: bool, percent: bool,
                          magnitude: bool = False) -> tuple[str, bool, int]:
    """TYP-01/TYP-12: apply confirmed numeric-cleaning rules.
    Returns (cleaned, is_percent_applied, magnitude_exponent)."""
    s = v.strip()
    negative = False
    if accounting_negative:
        m = _PAREN_NEG_RE.match(s)
        if m:
            s = m.group(1).strip()
            negative = True
    if s.endswith("-") and accounting_negative:  # trailing-minus variant
        s = s[:-1].strip()
        negative = True
    if currency:
        s = _CURRENCY_RE.sub("", s).strip()
    is_pct = False
    if percent and s.endswith("%"):
        s = s[:-1].strip()
        is_pct = True
    exp = 0
    if magnitude and s and s[-1].lower() in _MAGNITUDE:  # TYP-12
        exp = _MAGNITUDE[s[-1].lower()]
        s = s[:-1].strip()
    if thousands_sep:
        s = s.replace(thousands_sep, "")
    if negative and not s.startswith("-"):
        s = "-" + s
    return s, is_pct, exp


def to_int(value: CellValue, column: str, *, thousands_sep: str | None = None,
           currency: bool = False, accounting_negative: bool = False) -> int | None:
    if value is None:
        return None
    s, _, _ = _clean_numeric_string(str(value), thousands_sep=thousands_sep,
                                    currency=currency,
                                    accounting_negative=accounting_negative,
                                    percent=False)
    try:
        return int(s)
    except ValueError:
        raise RowError("TYP-01", column, value, "not parseable as integer")


def to_decimal(value: CellValue, column: str, *, thousands_sep: str | None = None,
               currency: bool = False, accounting_negative: bool = False,
               percent: bool = False, magnitude: bool = False,
               scale: int | None = None) -> Decimal | None:
    if value is None:
        return None
    s, is_pct, exp = _clean_numeric_string(str(value), thousands_sep=thousands_sep,
                                           currency=currency,
                                           accounting_negative=accounting_negative,
                                           percent=percent, magnitude=magnitude)
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise RowError("TYP-01", column, value, "not parseable as decimal")
    if exp:  # TYP-12: apply confirmed magnitude suffix
        d = d.scaleb(exp)
    if is_pct:
        d = d / Decimal(100)
    if scale is not None:
        quantized = d.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
        # TYP-08: silent rounding is forbidden — a value with MORE precision than
        # the target scale is a row-error unless the spec added an explicit rounding transform.
        if quantized != d:
            raise RowError("TYP-08", column, value,
                           f"exceeds declared scale {scale} (rounding must be an explicit transform)")
        d = quantized
    return d


def to_date(value: CellValue, column: str, *, formats: Sequence[str]) -> str | None:
    """TYP-03: parse with the spec's ordered format list; render ISO (TYP-05)."""
    if value is None:
        return None
    s = str(value)
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise RowError("TYP-03", column, value,
                   f"does not match declared format(s) {list(formats)}")


def to_datetime(value: CellValue, column: str, *, formats: Sequence[str],
                assume_tz: str | None = None, to_utc: bool = True) -> str | None:
    """TYP-04: naive values require assume_tz (a fixed-offset like '+05:30' or 'UTC')."""
    if value is None:
        return None
    s = str(value)
    parsed: datetime | None = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise RowError("TYP-03", column, value,
                       f"does not match declared format(s) {list(formats)}")
    if parsed.tzinfo is None:
        if assume_tz is None:
            raise RowError("TYP-04", column, value,
                           "naive datetime with no declared source timezone")
        parsed = parsed.replace(tzinfo=_parse_tz(assume_tz))
    if to_utc:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime(ISO8601_DATETIME)


def _parse_tz(tz: str) -> timezone:
    if tz.upper() == "UTC":
        return timezone.utc
    m = re.match(r"^([+-])(\d{2}):?(\d{2})$", tz)
    if not m:
        raise RunError("TYP-04", f"unsupported timezone declaration {tz!r} "
                                 "(use 'UTC' or a fixed offset like '+05:30')")
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))


def to_bool(value: CellValue, column: str, *, mapping: Mapping[object, bool],
            report: RunReport | None = None) -> bool | None:
    """TYP-06: only the confirmed vocabulary converts; anything else is a row-error."""
    if value is None:
        return None
    key = str(value)
    if key in mapping:
        return mapping[key]
    # Case-insensitive second chance — an auto-fix, counted per ERR-04 (TYP-10).
    for k, mapped in mapping.items():
        if key.casefold() == str(k).casefold():
            if report:
                report.warn(column, "TYP-10")
            return mapped
    raise RowError("TYP-06", column, value,
                   f"not in confirmed boolean vocabulary {sorted(map(str, mapping))}")


# ENC-06 mojibake signatures: characters that only appear when UTF-8 bytes were
# mis-decoded as Latin-1/CP1252 (Ã, Â, the â€ family, Cyrillic-range Ð/Ñ).
_MOJIBAKE_SIGNATURES = ("Ã", "Â", "â€", "Ð", "Ñ")


def repair_mojibake(value: CellValue, column: str,
                    report: RunReport | None = None) -> CellValue:
    """ENC-06: repair double-encoded UTF-8 baked into data (`JosÃ©` -> `José`).

    Heuristic and OPT-IN — the taxonomy default is pass-through-and-flag; a spec
    enables this only as a confirmed (or unattended-`unconfirmed`) decision.
    Safety guarantees so that applying it can never corrupt good data:
      * only strings carrying a mojibake signature are touched;
      * repair is the strict Latin-1 -> UTF-8 round-trip (no lossy error modes);
      * on ANY failure, or if the round-trip yields U+FFFD, the ORIGINAL value
        is returned unchanged. Repair must never lose data.
    Counts one ENC-06 warning per repaired value (ERR-04).
    """
    if value is None or not isinstance(value, str):
        return value
    if not any(sig in value for sig in _MOJIBAKE_SIGNATURES):
        return value
    try:
        repaired = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value  # not a clean Latin-1 -> UTF-8 round-trip; leave untouched
    if repaired == value or "�" in repaired:
        return value
    if report is not None:  # ERR-04: auto-fixes are counted
        report.warn(column, "ENC-06")
    return repaired


# ENC-08: a leading + or - is only a spreadsheet-formula risk when the value is
# NOT a plain signed numeric — `-500`, `-1,234.56` and scientific `-2.00E-02`
# all parse as numbers in every spreadsheet and must never be touched.
# (`-INF` is NOT exempt: Excel reads it as a formula and mangles it to #NAME?.)
_SIGNED_NUMBER_RE = re.compile(r"^[+-][0-9.,]*\d([eE][+-]?\d+)?$")


def neutralize_formula(value: str, column: str,
                       report: RunReport | None = None) -> str:
    """ENC-08: prefix a literal apostrophe to a rendered output cell that a
    spreadsheet would execute as a formula (leading `=` `@` TAB CR always;
    `+`/`-` only for non-numeric values). OPT-IN — the taxonomy default is
    pass-through-and-flag; a spec enables this via
    `policies.formula_injection: neutralize` for outputs destined for
    spreadsheet users. Counts one ENC-08 warning per neutralized cell (ERR-04).
    """
    if not value:
        return value
    ch = value[0]
    # A bare "-"/"+" (len 1) is inert in every spreadsheet — common as a kept
    # dash sentinel — so only a sign WITH a non-numeric tail is a risk.
    if ch in "=@\t\r" or (ch in "+-" and len(value) > 1
                          and not _SIGNED_NUMBER_RE.match(value)):
        if report is not None:
            report.warn(column, "ENC-08")
        return "'" + value
    return value


def skip_if(value: T, condition: object, code: str = "STR-06", reason: str = "") -> T:
    """STR-06 helper: exclude the current row when `condition` is true, else
    return `value` untouched. Usable both as a statement (value=None) in
    compiler-emitted skip guards and inside a spec `expr` transform (where a
    `raise` cannot appear in the expression body). The SkipRow handler counts
    the exclusion as a warning under `code` — never silent."""
    if condition:
        raise SkipRow(code, reason)
    return value


def concat(values: Sequence[CellValue], column: str, *, sep: str = "") -> str | None:
    """Join multiple source values. NUL-05 policy: SQL semantics — any null
    operand yields null (overrides are per-mapping, visible in the spec)."""
    if any(v is None for v in values):
        return None
    return sep.join(str(v) for v in values)


def check_length(value: CellValue, column: str, *, max_length: int) -> str | None:
    if value is None:
        return None
    s = str(value)
    if len(s) > max_length:  # TYP-11: never silently truncate
        raise RowError("TYP-11", column, value,
                       f"length {len(s)} exceeds declared max {max_length}")
    return s


def check_range(value: int | float | Decimal | None, column: str,
                *, min: int | float | Decimal | None = None,
                max: int | float | Decimal | None = None
                ) -> int | float | Decimal | None:
    if value is None:
        return None
    if min is not None and value < min:
        raise RowError("TYP-09", column, value, f"below declared minimum {min}")
    if max is not None and value > max:
        raise RowError("TYP-09", column, value, f"above declared maximum {max}")
    return value


def not_null(value: T | None, column: str) -> T:
    if value is None:  # NUL-04
        raise RowError("NUL-04", column, value, "null arrived at non-nullable target")
    return value


def format_datetime(value: datetime | date | str | None, column: str,
                    *, fmt: str = ISO8601_DATE) -> str | None:
    """TYP-05: one canonical rendering, declared in the spec."""
    if value is None:
        return None
    if isinstance(value, str):
        return value  # already rendered by to_date/to_datetime
    return value.strftime(fmt)
