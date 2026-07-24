"""etl_runtime — the I/O driver for pipelines produced by the etl-generator skill.

Two-module runtime, split on the effect boundary:

  * `etl_coercers` — the deterministic core: cleaning, null resolution, type
    coercion, the report accumulator. No I/O, no clock; the only effect is
    counting into an explicitly-passed `RunReport`.
  * `etl_runtime` (this file) — the impure shell: file reading, the row loop,
    dispositions, atomic output, report/manifest writing (the one wall-clock
    read lives in the manifest timestamp).

Generated pipelines import ONLY `etl_runtime`; the core's public API is
re-exported here, so `rt.to_decimal(...)` etc. keep working unchanged. The two
files ship together (copy both beside the generated pipeline).

Every error/warning code refers to an entry in the ETL Failure-Mode Taxonomy
(references/taxonomy.md in the skill). Stdlib only — no third-party deps.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as Decimal
from typing import Callable, Mapping, Sequence, TypedDict

from etl_coercers import (
    ISO8601_DATE as ISO8601_DATE,
    ISO8601_DATETIME as ISO8601_DATETIME,
    TAXONOMY_VERSION as TAXONOMY_VERSION,
    AnnotationRecord as AnnotationRecord,
    CellValue as CellValue,
    ChangeRecord as ChangeRecord,
    DuplicateRecord as DuplicateRecord,
    RowError as RowError,
    RowErrorRecord as RowErrorRecord,
    RunError as RunError,
    RunErrorInfo as RunErrorInfo,
    RunReport as RunReport,
    SkipRow as SkipRow,
    SummaryDict as SummaryDict,
    check_length as check_length,
    check_range as check_range,
    clean_text as clean_text,
    concat as concat,
    format_datetime as format_datetime,
    not_null as not_null,
    repair_mojibake as repair_mojibake,
    resolve_null as resolve_null,
    skip_if as skip_if,
    to_bool as to_bool,
    to_date as to_date,
    to_datetime as to_datetime,
    to_decimal as to_decimal,
    to_int as to_int,
)

RUNTIME_VERSION = "0.6.0"


# ---------------------------------------------------------------------------
# The pipeline-config contract (embedded by the compiler; consumed here).
# total=False throughout: the driver reads with .get() defaults and fails loud
# on genuinely required keys, tolerating hand-written partial configs.
# ---------------------------------------------------------------------------

class ErrorBudgetDict(TypedDict, total=False):
    """ERR-02: row-error tolerance before the run converts to hard failure."""
    percent: float
    min_rows: int


class PoliciesDict(TypedDict, total=False):
    """Dataset-level policies, mirroring the spec's `policies:` block."""
    unicode_normalization: str
    strip_control_chars: bool
    normalize_unicode_whitespace: bool
    trim_whitespace: bool
    empty_string_is_null: bool
    null_propagation: str
    datetime_rendering: str
    error_disposition: str
    error_budget: ErrorBudgetDict
    duplicate_rows: str
    sentinels: dict[str, list[str]]


class PipelineConfig(TypedDict, total=False):
    """The resolved spec a generated pipeline embeds as its CONFIG constant."""
    name: str
    spec_version: str
    generator_version: str
    encoding: str
    delimiter: str
    quotechar: str
    expected_columns: list[str]
    output_columns: list[str]
    policies: PoliciesDict


# A row after driver-side cleaning/null resolution, as seen by transforms.
CleanRow = dict[str, str | None]
TransformRowFn = Callable[[CleanRow, RunReport], Mapping[str, CellValue]]
FieldTransform = tuple[str, Callable[[CleanRow, RunReport], CellValue]]
RowGuardsFn = Callable[[CleanRow, RunReport], None]


@dataclass
class RunResult:
    exit_code: int
    report: RunReport
    out_dir: str


# ---------------------------------------------------------------------------
# File reading (ENC-01 / ENC-02 / STR-07)
# ---------------------------------------------------------------------------

def read_text_with_policy(path: str, encoding: str, report: RunReport) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):  # ENC-02
        raw = raw[3:]
        report.warn("<file>", "ENC-02")
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as e:
        # ENC-01: never decode with replacement characters silently — that's data loss.
        raise RunError("ENC-01", f"file is not valid {encoding}: {e}. "
                                 "Re-profile the file and declare the correct encoding in the spec.")
    if "\r" in text:  # STR-07: counted, NOT rewritten — csv.reader handles all of
        # \n, \r\n and \r as row terminators, and a blanket replace would corrupt
        # legitimate CRLF inside quoted fields.
        report.warn("<file>", "STR-07")
    return text


# ---------------------------------------------------------------------------
# Pipeline driver (STR-02, KEY-02/03, ERR-01/02/05/06)
# ---------------------------------------------------------------------------

DISPOSITIONS = ("quarantine", "fail-fast", "annotate")  # ERR-01 decision space


def run_pipeline(*, input_path: str, out_dir: str, config: PipelineConfig,
                 transform_row: TransformRowFn | None = None,
                 field_transforms: Sequence[FieldTransform] | None = None,
                 row_guards: RowGuardsFn | None = None) -> RunResult:
    """ERR-01 dispositions: `quarantine` (default) and `fail-fast` use
    `transform_row` (built from `field_transforms` if absent). `annotate`
    REQUIRES `field_transforms` — per-field granularity is what lets one bad
    field be NULLed-and-ledgered while the rest of the row survives."""
    report = RunReport()
    os.makedirs(out_dir, exist_ok=True)

    def abort(code: str, message: str) -> RunResult:
        # The single terminate-and-report contract: record the run error, write
        # all reports (which also removes any stale output.csv), return exit 1.
        report.run_error = {"code": code, "message": message}
        _write_reports(out_dir, report, config, input_path)
        return RunResult(1, report, out_dir)

    try:
        return _run_pipeline(input_path, out_dir, config, transform_row,
                             field_transforms, row_guards, report, abort)
    except RunError as e:
        # ERR-05: a failed run leaves no partial output — but always the reports.
        return abort(e.code, e.message)
    except Exception as e:  # noqa: BLE001 — ERR-05 applies to unexpected bugs too:
        # a broken expr / config typo must still leave reports, not a bare traceback.
        return abort("ERR-05", f"unexpected {type(e).__name__}: {e}")


def _run_pipeline(input_path: str, out_dir: str, config: PipelineConfig,
                  transform_row: TransformRowFn | None,
                  field_transforms: Sequence[FieldTransform] | None,
                  row_guards: RowGuardsFn | None,
                  report: RunReport,
                  abort: Callable[[str, str], RunResult]) -> RunResult:
    pol = config.get("policies") or PoliciesDict()
    sentinels_by_col = {c: tuple(v) for c, v in (pol.get("sentinels") or {}).items()}
    disposition = pol.get("error_disposition", "quarantine")  # ERR-01
    if disposition not in DISPOSITIONS:
        # Never silently fall back — an unknown disposition means the spec and
        # this runtime disagree about semantics.
        raise RunError("ERR-01", f"unknown error_disposition {disposition!r} "
                                 f"(this runtime supports {DISPOSITIONS})")
    if disposition == "annotate" and not field_transforms:
        raise RunError("ERR-01", "annotate disposition requires field_transforms "
                                 "(per-field granularity); regenerate the pipeline "
                                 "with compiler >= 0.3.0")
    run_transform = transform_row
    if run_transform is None:
        if not field_transforms:
            raise RunError("ERR-01", "run_pipeline needs transform_row or field_transforms")
        fts = list(field_transforms)
        guards = row_guards

        def _built(row: CleanRow, rep: RunReport) -> Mapping[str, CellValue]:
            if guards:
                guards(row, rep)
            return {t: fn(row, rep) for t, fn in fts}

        run_transform = _built
    budget = pol.get("error_budget") or ErrorBudgetDict(percent=5, min_rows=100)  # ERR-02

    text = read_text_with_policy(input_path, config.get("encoding", "utf-8"), report)
    # newline="" lets csv own row-termination: it accepts \n, \r\n and lone \r
    # (old-Mac) alike AND preserves CRLF inside quoted fields (STR-07 is counted,
    # never rewritten).
    reader = csv.reader(io.StringIO(text, newline=""),
                        delimiter=config.get("delimiter", ","),
                        quotechar=config.get("quotechar", '"'))
    rows = list(reader)
    if not rows:
        raise RunError("STR-04", "file is empty — no header row found")

    header = [h.strip() for h in rows[0]]
    expected = config["expected_columns"]
    missing = [c for c in expected if c not in header]
    if missing:  # KEY-02
        raise RunError("KEY-02", f"expected column(s) missing from input: {missing}")
    dup_headers = sorted({c for c in expected if header.count(c) > 1})
    if dup_headers:  # STR-04: which occurrence to read from is ambiguous — never guess
        raise RunError("STR-04", f"duplicate header column name(s): {dup_headers}")
    extra = [c for c in header if c not in expected]
    if extra:  # KEY-03
        report.warn("<file>", "KEY-03")
    col_index = {c: header.index(c) for c in expected}

    out_columns = config["output_columns"]
    out_rows: list[list[str]] = []
    data_rows = rows[1:]
    report.rows_in = len(data_rows)

    duplicate_policy = pol.get("duplicate_rows", "keep")  # STR-05: keep | drop_exact
    seen_rows: dict[tuple[str, ...], int] = {}

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
            try:
                if len(raw) != len(header):  # STR-02
                    raise RowError("STR-02", "<row>", None,
                                   f"expected {len(header)} fields, got {len(raw)}")
                row: CleanRow = {}
                for cname, idx in col_index.items():
                    v = clean_text(raw[idx], cname, report,
                                   normalization=pol.get("unicode_normalization", "NFC"),
                                   strip_control=pol.get("strip_control_chars", True),
                                   normalize_ws=pol.get("normalize_unicode_whitespace", True))
                    row[cname] = resolve_null(v, cname, report,
                                              trim=pol.get("trim_whitespace", True),
                                              empty_is_null=pol.get("empty_string_is_null", True),
                                              sentinels=sentinels_by_col.get(cname, ()))
                out: Mapping[str, CellValue]
                if disposition == "annotate":
                    assert field_transforms is not None  # validated above
                    out, changes = _transform_annotating(row, report, field_transforms,
                                                         row_guards)
                    if changes:
                        report.annotate_row(i, changes)
                else:
                    out = run_transform(row, report)
                out_rows.append([_render(out.get(c)) for c in out_columns])
            except (SkipRow, RowError):
                raise
            except Exception as e:  # noqa: BLE001 — ERR-07: unexpected row-level
                # failure (a buggy expr, an unforeseen data shape). Quarantine the
                # row and continue — quarantine-not-abort applies to bugs too; a
                # transform broken for EVERY row is promoted to run failure by the
                # ERR-02 budget / end-of-run check. Under annotate this quarantines
                # the whole row: a failure with no attributable field must never be
                # repaired into clean output.
                raise RowError("ERR-07", "<row>", None,
                               f"unexpected {type(e).__name__}: {e}") from e
        except SkipRow as skip:
            report.warn("<row>", skip.code)
        except RowError as err:
            if disposition == "fail-fast":
                return abort(err.code, f"fail-fast on row {i}: {err.message}")
            report.add_row_error(i, raw, err, expected)
            # ERR-02: row tolerance must not mask systemic failure. min_rows is a
            # minimum SAMPLE size (small files judged at end-of-run), not a
            # minimum failure count.
            if (report.rows_in >= budget.get("min_rows", 100)
                    and 100.0 * len(report.quarantined) / report.rows_in > budget.get("percent", 5)):
                return abort("ERR-02",
                             f"error budget exceeded: {len(report.quarantined)} of "
                             f"{report.rows_in} rows failed (> {budget.get('percent', 5)}%)")

    # ERR-02 end-of-run check: whatever the budget, a run in which EVERY row
    # failed is systemic failure, never success-with-warnings. (A non-empty
    # quarantine implies rows_in >= 1, so no separate rows_in guard is needed.)
    if not out_rows and report.quarantined:
        return abort("ERR-02", f"all {report.rows_in} data row(s) failed")

    report.rows_out = len(out_rows)
    # ERR-05: atomic output — write to temp, promote on success only.
    _atomic_write_csv(os.path.join(out_dir, "output.csv"), out_columns, out_rows)
    _write_reports(out_dir, report, config, input_path)
    # exit 2 = "investigate": quarantined rows OR annotated (repaired) rows.
    return RunResult(2 if (report.quarantined or report.annotations) else 0,
                     report, out_dir)


def _transform_annotating(row: CleanRow, report: RunReport,
                          field_transforms: Sequence[FieldTransform],
                          row_guards: RowGuardsFn | None
                          ) -> tuple[dict[str, CellValue], list[ChangeRecord]]:
    """ERR-01 option (c): per-field transform. A content RowError NULLs that
    field and ledgers {field, change: NULLED, reason: <taxonomy id>}; the row
    loads. NUL-04 (declared non-nullable) re-raises — a NOT NULL target cannot
    be repaired by nulling, so the whole row quarantines."""
    if row_guards:
        row_guards(row, report)  # SkipRow propagates to the driver
    out: dict[str, CellValue] = {}
    changes: list[ChangeRecord] = []
    for target, fn in field_transforms:
        try:
            out[target] = fn(row, report)
        except RowError as err:
            if err.code == "NUL-04":
                raise
            out[target] = None
            changes.append({"field": target, "change": "NULLED", "reason": err.code})
    return out, changes


def _render(v: CellValue) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        return format(v, "f")
    return str(v)


def _atomic_write_csv(path: str, header: Sequence[str],
                      rows: Sequence[Sequence[str]]) -> None:
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


def _write_reports(out_dir: str, report: RunReport, config: PipelineConfig,
                   input_path: str) -> None:
    """ERR-03: all three granularities, always. ERR-06: manifest, always."""
    if report.run_error:
        # A failed run must not leave a previous run's output.csv sitting next to
        # a failing manifest — stale data would read as current.
        try:
            os.unlink(os.path.join(out_dir, "output.csv"))
        except OSError:
            pass
    with open(os.path.join(out_dir, "errors.jsonl"), "w", encoding="utf-8") as f:
        for e in report.row_errors:
            f.write(json.dumps(e) + "\n")
    if report.annotations:  # ERR-01(c): the per-row change ledger
        with open(os.path.join(out_dir, "changes.jsonl"), "w", encoding="utf-8") as f:
            for a in report.annotations:
                f.write(json.dumps(a) + "\n")
    if report.quarantined:
        with open(os.path.join(out_dir, "quarantine.csv"), "w", newline="",
                  encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["__row_number__", "__raw_row__"])
            for row_number, raw in report.quarantined:
                w.writerow([row_number, json.dumps(raw)])
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(report.summary(), f, indent=2)
    with open(input_path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    # ERR-06: spec provenance — hash of the resolved config the pipeline embeds.
    spec_sha = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"),
                                         default=str).encode("utf-8")).hexdigest()
    manifest: dict[str, object] = {
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
