"""etl_runtime — hardened primitives for generated ETL pipelines.

This module is the single shared implementation of edge-case semantics for
pipelines produced by the etl-generator skill. Every error/warning code refers
to an entry in the ETL Failure-Mode Taxonomy (references/taxonomy.md in the
skill). Generated pipelines stay thin; the semantics live here, once, tested.

Stdlib only — no third-party dependencies.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

RUNTIME_VERSION = "0.2.0"
TAXONOMY_VERSION = "0.2"
ISO8601_DATE = "%Y-%m-%d"
ISO8601_DATETIME = "%Y-%m-%dT%H:%M:%S%z"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class RowError(Exception):
    """A row-level failure. code is a taxonomy ID (e.g. 'TYP-03')."""

    def __init__(self, code: str, column: str, value, message: str):
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
# Report (ERR-03 / ERR-04 / ERR-06)
# ---------------------------------------------------------------------------

@dataclass
class RunReport:
    rows_in: int = 0
    rows_out: int = 0
    row_errors: list = field(default_factory=list)      # per-row records
    quarantined: list = field(default_factory=list)     # raw input rows (lists)
    warnings: Counter = field(default_factory=Counter)  # (column, code) -> count
    duplicates: list = field(default_factory=list)      # STR-05: exact-duplicate sightings
    run_error: dict | None = None

    def warn(self, column: str, code: str, n: int = 1):
        """ERR-04: every auto-fix is counted, never silent."""
        if n:
            self.warnings[(column or "<row>", code)] += n

    def add_row_error(self, row_number: int, raw_row: list, err: RowError):
        self.row_errors.append({
            "row_number": row_number,
            "column": err.column,
            "error_code": err.code,
            "offending_value": None if err.value is None else str(err.value)[:500],
            "message": err.message,
        })
        self.quarantined.append((row_number, raw_row))

    # -- aggregates (ERR-03 ii) --
    def error_aggregates(self) -> dict:
        agg: Counter = Counter()
        for e in self.row_errors:
            agg[(e["error_code"], e["column"])] += 1
        return {f"{code}:{col}": n for (code, col), n in sorted(agg.items())}

    def warning_aggregates(self) -> dict:
        return {f"{code}:{col}": n
                for (col, code), n in sorted(self.warnings.items(), key=lambda kv: kv[0][1])}

    def summary(self) -> dict:
        return {
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "rows_quarantined": len(self.quarantined),
            "distinct_error_types": len(self.error_aggregates()),
            "errors_by_type": self.error_aggregates(),
            "warnings_by_type": self.warning_aggregates(),
            "exact_duplicate_rows": self.duplicates,
            "run_error": self.run_error,
        }


@dataclass
class RunResult:
    exit_code: int
    report: RunReport
    out_dir: str


# ---------------------------------------------------------------------------
# Text cleaning (ENC-03 / ENC-04 / ENC-05) and null resolution (NUL-01/02/03)
# ---------------------------------------------------------------------------

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Unicode whitespace variants -> ASCII space; zero-width & BOM chars -> removed. (ENC-05)
_UNICODE_SPACE_RE = re.compile(
    "[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]"
)
_ZERO_WIDTH_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")


def clean_text(value: str | None, column: str, report: RunReport | None,
               *, normalization: str = "NFC", strip_control: bool = True,
               normalize_ws: bool = True) -> str | None:
    """Apply ENC-class fixes per policy. Counts every change as a warning."""
    if value is None:
        return None
    v = value
    if normalization:  # ENC-03
        nv = unicodedata.normalize(normalization, v)
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


# ENC-06: mojibake repair (opt-in). A previous pipeline decoded UTF-8 bytes with
# the wrong codec (latin-1/cp1252) and re-saved, leaving signature sequences like
# "Ã©" for "é" or "â€™" for "'". Repair reverses that: re-encode the text as
# latin-1 to recover the original bytes, then decode them as UTF-8.
# Applied ONLY when the spec opts in for a column, and ONLY to values that carry
# the mojibake signature and round-trip cleanly — heuristic repair of clean text
# corrupts it, which is why ENC-06's house default is pass-through-and-flag.
# Signature: a 2-byte-UTF-8 lead byte mis-decoded to U+00C2..U+00DF followed by a
# continuation byte mis-decoded to U+0080..U+00BF.
_MOJIBAKE_SIGNATURE_RE = re.compile("[\u00c2-\u00df][\u0080-\u00bf]")


def repair_mojibake(value, column: str, report: RunReport | None = None,
                    *, normalization: str = "NFC") -> str | None:
    """ENC-06: reverse a prior UTF-8/latin-1 mis-decode. Counts every repair."""
    if value is None:
        return None
    s = str(value)
    if not _MOJIBAKE_SIGNATURE_RE.search(s):
        return s  # no signature -> not mojibake, never touch clean text
    try:
        repaired = s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s  # not cleanly reversible -> leave as-is rather than corrupt
    if normalization:
        repaired = unicodedata.normalize(normalization, repaired)
    if repaired != s and report is not None:
        report.warn(column, "ENC-06")
    return repaired


# ---------------------------------------------------------------------------
# Coercers (TYP-*). All pass None through. All raise RowError on failure.
# ---------------------------------------------------------------------------

_PAREN_NEG_RE = re.compile(r"^\((.*)\)$")
_CURRENCY_RE = re.compile(r"^[\s]*[$€£¥₹]")
# TYP-12: magnitude/scale suffixes (10.00K, 1.2M). Applied only when the spec confirms it.
_MAGNITUDE = {"k": 3, "m": 6, "b": 9, "g": 9, "t": 12}


def _clean_numeric_string(v: str, column: str, *, thousands_sep, currency,
                          accounting_negative, percent, magnitude=False
                          ) -> tuple[str, bool, int]:
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


def to_int(value, column: str, *, thousands_sep=None, currency=False,
           accounting_negative=False) -> int | None:
    if value is None:
        return None
    s, _, _ = _clean_numeric_string(str(value), column, thousands_sep=thousands_sep,
                                    currency=currency, accounting_negative=accounting_negative,
                                    percent=False)
    try:
        return int(s)
    except ValueError:
        raise RowError("TYP-01", column, value, "not parseable as integer")


def to_decimal(value, column: str, *, thousands_sep=None, currency=False,
               accounting_negative=False, percent=False, magnitude=False,
               scale=None) -> Decimal | None:
    if value is None:
        return None
    s, is_pct, exp = _clean_numeric_string(str(value), column, thousands_sep=thousands_sep,
                                           currency=currency, accounting_negative=accounting_negative,
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


def to_date(value, column: str, *, formats) -> str | None:
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
                   f"does not match declared format(s) {formats}")


def to_datetime(value, column: str, *, formats, assume_tz=None, to_utc=True) -> str | None:
    """TYP-04: naive values require assume_tz (a fixed-offset like '+05:30' or 'UTC')."""
    if value is None:
        return None
    s = str(value)
    parsed = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise RowError("TYP-03", column, value,
                       f"does not match declared format(s) {formats}")
    if parsed.tzinfo is None:
        if assume_tz is None:
            raise RowError("TYP-04", column, value,
                           "naive datetime with no declared source timezone")
        parsed = parsed.replace(tzinfo=_parse_tz(assume_tz))
    if to_utc:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime(ISO8601_DATETIME)


def _parse_tz(tz: str):
    if tz.upper() == "UTC":
        return timezone.utc
    m = re.match(r"^([+-])(\d{2}):?(\d{2})$", tz)
    if not m:
        raise RunError("TYP-04", f"unsupported timezone declaration {tz!r} "
                                 "(use 'UTC' or a fixed offset like '+05:30')")
    sign = 1 if m.group(1) == "+" else -1
    from datetime import timedelta
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))


def to_bool(value, column: str, *, mapping, report: RunReport | None = None) -> bool | None:
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


def check_length(value, column: str, *, max_length) -> str | None:
    if value is None:
        return None
    s = str(value)
    if len(s) > max_length:  # TYP-11: never silently truncate
        raise RowError("TYP-11", column, value,
                       f"length {len(s)} exceeds declared max {max_length}")
    return s


def check_range(value, column: str, *, min=None, max=None):
    if value is None:
        return None
    if min is not None and value < min:
        raise RowError("TYP-09", column, value, f"below declared minimum {min}")
    if max is not None and value > max:
        raise RowError("TYP-09", column, value, f"above declared maximum {max}")
    return value


def not_null(value, column: str):
    if value is None:  # NUL-04
        raise RowError("NUL-04", column, value, "null arrived at non-nullable target")
    return value


def format_datetime(value, column: str, *, fmt=ISO8601_DATE):
    """TYP-05: one canonical rendering, declared in the spec."""
    if value is None:
        return None
    if isinstance(value, str):
        return value  # already rendered by to_date/to_datetime
    return value.strftime(fmt)


# ---------------------------------------------------------------------------
# File reading (ENC-01 / ENC-02 / STR-07)
# ---------------------------------------------------------------------------

def read_text_with_policy(path: str, encoding: str, report: RunReport) -> str:
    raw = open(path, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):  # ENC-02
        raw = raw[3:]
        report.warn("<file>", "ENC-02")
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as e:
        # ENC-01: never decode with replacement characters silently — that's data loss.
        raise RunError("ENC-01", f"file is not valid {encoding}: {e}. "
                                 "Re-profile the file and declare the correct encoding in the spec.")
    if "\r" in text:  # STR-07
        report.warn("<file>", "STR-07")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


# ---------------------------------------------------------------------------
# Pipeline driver (STR-02, KEY-02/03, ERR-01/02/05/06)
# ---------------------------------------------------------------------------

def run_pipeline(*, input_path: str, out_dir: str, config: dict, transform_row) -> RunResult:
    report = RunReport()
    os.makedirs(out_dir, exist_ok=True)
    try:
        return _run_pipeline(input_path, out_dir, config, transform_row, report)
    except RunError as e:
        # ERR-05: a failed run leaves no partial output — but always the reports.
        report.run_error = {"code": e.code, "message": e.message}
        _write_reports(out_dir, report, config, input_path)
        return RunResult(1, report, out_dir)


def _run_pipeline(input_path: str, out_dir: str, config: dict, transform_row,
                  report: RunReport) -> RunResult:
    pol = config.get("policies", {})
    sentinels_by_col = {c: tuple(v) for c, v in pol.get("sentinels", {}).items()}
    disposition = pol.get("error_disposition", "quarantine")  # ERR-01
    budget = pol.get("error_budget", {"percent": 5, "min_rows": 100})  # ERR-02

    text = read_text_with_policy(input_path, config.get("encoding", "utf-8"), report)
    reader = csv.reader(io.StringIO(text), delimiter=config.get("delimiter", ","),
                        quotechar=config.get("quotechar", '"'))
    rows = list(reader)
    if not rows:
        raise RunError("STR-04", "file is empty — no header row found")

    header = [h.strip() for h in rows[0]]
    expected = config["expected_columns"]
    missing = [c for c in expected if c not in header]
    if missing:  # KEY-02
        raise RunError("KEY-02", f"expected column(s) missing from input: {missing}")
    extra = [c for c in header if c not in expected]
    if extra:  # KEY-03
        report.warn("<file>", "KEY-03")
    col_index = {c: header.index(c) for c in expected}

    out_columns = config["output_columns"]
    out_rows = []
    data_rows = rows[1:]
    report.rows_in = len(data_rows)

    duplicate_policy = pol.get("duplicate_rows", "keep")  # STR-05: keep | drop_exact
    seen_rows: dict = {}

    for i, raw in enumerate(data_rows, start=2):  # row numbers are 1-based incl. header
        if not any(f.strip() for f in raw):
            report.warn("<row>", "STR-06")  # blank row, skipped and counted
            continue
        rkey = tuple(raw)
        if rkey in seen_rows:  # STR-05: kept by default, but never unreported
            report.warn("<row>", "STR-05")
            report.duplicates.append({"row_number": i, "first_seen_row": seen_rows[rkey]})
            if duplicate_policy == "drop_exact":
                continue
        else:
            seen_rows[rkey] = i
        try:
            if len(raw) != len(header):  # STR-02
                raise RowError("STR-02", "<row>", None,
                               f"expected {len(header)} fields, got {len(raw)}")
            row = {}
            for cname, idx in col_index.items():
                v = clean_text(raw[idx], cname, report,
                               normalization=pol.get("unicode_normalization", "NFC"),
                               strip_control=pol.get("strip_control_chars", True),
                               normalize_ws=pol.get("normalize_unicode_whitespace", True))
                row[cname] = resolve_null(v, cname, report,
                                          trim=pol.get("trim_whitespace", True),
                                          empty_is_null=pol.get("empty_string_is_null", True),
                                          sentinels=sentinels_by_col.get(cname, ()))
            out = transform_row(row, report)
            out_rows.append([_render(out.get(c)) for c in out_columns])
        except SkipRow as skip:
            report.warn("<row>", skip.code)
        except RowError as err:
            if disposition == "fail-fast":
                report.run_error = {"code": err.code,
                                    "message": f"fail-fast on row {i}: {err.message}"}
                _write_reports(out_dir, report, config, input_path)
                return RunResult(1, report, out_dir)
            report.add_row_error(i, raw, err)
            # ERR-02: row tolerance must not mask systemic failure.
            if (len(report.quarantined) >= budget.get("min_rows", 100)
                    and report.rows_in
                    and 100.0 * len(report.quarantined) / report.rows_in > budget.get("percent", 5)):
                report.run_error = {
                    "code": "ERR-02",
                    "message": (f"error budget exceeded: {len(report.quarantined)} of "
                                f"{report.rows_in} rows failed (> {budget.get('percent', 5)}%)"),
                }
                _write_reports(out_dir, report, config, input_path)
                return RunResult(1, report, out_dir)

    report.rows_out = len(out_rows)
    # ERR-05: atomic output — write to temp, promote on success only.
    _atomic_write_csv(os.path.join(out_dir, "output.csv"), out_columns, out_rows)
    _write_reports(out_dir, report, config, input_path)
    return RunResult(2 if report.quarantined else 0, report, out_dir)


def _render(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        return format(v, "f")
    return str(v)


def _atomic_write_csv(path: str, header: list, rows: list):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_reports(out_dir: str, report: RunReport, config: dict, input_path: str):
    """ERR-03: all three granularities, always. ERR-06: manifest, always."""
    with open(os.path.join(out_dir, "errors.jsonl"), "w", encoding="utf-8") as f:
        for e in report.row_errors:
            f.write(json.dumps(e) + "\n")
    if report.quarantined:
        with open(os.path.join(out_dir, "quarantine.csv"), "w", newline="",
                  encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["__row_number__", "__raw_row__"])
            for row_number, raw in report.quarantined:
                w.writerow([row_number, json.dumps(raw)])
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(report.summary(), f, indent=2)
    sha = hashlib.sha256(open(input_path, "rb").read()).hexdigest()
    # ERR-06: spec provenance — hash of the resolved config the pipeline embeds.
    spec_sha = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"),
                                         default=str).encode("utf-8")).hexdigest()
    manifest = {
        "pipeline": config.get("name"),
        "runtime_version": RUNTIME_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "spec_version": config.get("spec_version"),
        "spec_sha256": spec_sha,
        "generator_version": config.get("generator_version"),
        "input_file": os.path.basename(input_path),
        "input_sha256": sha,
        "rows_in": report.rows_in,
        "rows_out": report.rows_out,
        "rows_quarantined": len(report.quarantined),
        "run_error": report.run_error,
        "completed_at_utc": datetime.now(timezone.utc).strftime(ISO8601_DATETIME),
        "effective_policies": config.get("policies", {}),
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, default=str)
