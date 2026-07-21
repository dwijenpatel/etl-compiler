"""Unit tests for etl_runtime — the single shared implementation of taxonomy semantics.

Run:  python3 -m unittest discover -s tests -v

Sections mirror the runtime: text cleaning, null resolution, coercers, file
reading, and the pipeline driver. Test names carry the taxonomy IDs they pin.
"""
import csv
import hashlib
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "skill", "etl-generator", "assets"))
import etl_runtime as rt


class TestCleanText(unittest.TestCase):
    def test_enc03_nfc_normalization_applied_and_counted(self):
        report = rt.RunReport()
        v = rt.clean_text("José", "name", report)  # decomposed é
        self.assertEqual(v, "José")
        self.assertEqual(report.warnings[("name", "ENC-03")], 1)

    def test_enc04_control_chars_stripped_and_counted(self):
        report = rt.RunReport()
        v = rt.clean_text("ab\x00c\x1fd", "col", report)
        self.assertEqual(v, "abcd")
        self.assertEqual(report.warnings[("col", "ENC-04")], 1)

    def test_enc05_unicode_whitespace_mapped_and_zero_width_removed(self):
        report = rt.RunReport()
        v = rt.clean_text("a b​c", "col", report)
        self.assertEqual(v, "a bc")
        self.assertEqual(report.warnings[("col", "ENC-05")], 1)

    def test_clean_value_produces_no_warnings(self):
        report = rt.RunReport()
        self.assertEqual(rt.clean_text("plain", "col", report), "plain")
        self.assertEqual(sum(report.warnings.values()), 0)

    def test_none_passes_through(self):
        self.assertIsNone(rt.clean_text(None, "col", None))


class TestResolveNull(unittest.TestCase):
    def test_nul02_whitespace_only_becomes_null_and_counted(self):
        report = rt.RunReport()
        self.assertIsNone(rt.resolve_null("   ", "col", report))
        self.assertEqual(report.warnings[("col", "NUL-02")], 1)

    def test_nul03_sentinel_becomes_null_and_counted(self):
        report = rt.RunReport()
        self.assertIsNone(rt.resolve_null("N/A", "col", report, sentinels=("N/A",)))
        self.assertEqual(report.warnings[("col", "NUL-03")], 1)

    def test_nul01_empty_string_becomes_null_per_policy(self):
        report = rt.RunReport()
        self.assertIsNone(rt.resolve_null("", "col", report))
        self.assertEqual(rt.resolve_null("", "col", report, empty_is_null=False), "")

    def test_plain_value_passes_through_uncounted(self):
        report = rt.RunReport()
        self.assertEqual(rt.resolve_null("Gold", "col", report), "Gold")
        self.assertEqual(sum(report.warnings.values()), 0)

    def test_typ10_nonempty_trim_is_counted(self):
        # ERR-04: every auto-fix is counted. A trim that changes a non-empty
        # value is a TYP-10 fix, not silence.
        report = rt.RunReport()
        self.assertEqual(rt.resolve_null(" Gold ", "col", report), "Gold")
        self.assertEqual(report.warnings[("col", "TYP-10")], 1)
        self.assertEqual(report.warnings[("col", "NUL-02")], 0)


class TestCoercers(unittest.TestCase):
    def test_typ01_currency_thousands_decimal(self):
        d = rt.to_decimal("$1,234.56", "amt", thousands_sep=",", currency=True)
        self.assertEqual(d, Decimal("1234.56"))

    def test_typ01_accounting_negative(self):
        d = rt.to_decimal("(500)", "amt", accounting_negative=True, scale=2)
        self.assertEqual(d, Decimal("-500.00"))

    def test_typ01_percent_to_fraction(self):
        self.assertEqual(rt.to_decimal("12%", "pct", percent=True), Decimal("0.12"))

    def test_typ12_magnitude_suffix_scales(self):
        # TYP-12: NOAA-style scaled strings. Only applied when the spec confirms it.
        self.assertEqual(rt.to_decimal("10.00K", "damage", magnitude=True), Decimal("10000.00"))
        self.assertEqual(rt.to_decimal("1.2M", "damage", magnitude=True), Decimal("1200000"))
        self.assertEqual(rt.to_decimal("0.00K", "damage", magnitude=True), Decimal("0.00"))

    def test_typ12_magnitude_not_applied_by_default(self):
        # Without the flag a trailing letter is a parse error, never silently dropped.
        with self.assertRaises(rt.RowError):
            rt.to_decimal("10.00K", "damage")

    def test_typ01_garbage_raises_row_error(self):
        with self.assertRaises(rt.RowError) as ctx:
            rt.to_decimal("abc", "amt")
        self.assertEqual(ctx.exception.code, "TYP-01")

    def test_typ08_excess_scale_is_row_error_not_silent_rounding(self):
        with self.assertRaises(rt.RowError) as ctx:
            rt.to_decimal("1.239", "amt", scale=2)
        self.assertEqual(ctx.exception.code, "TYP-08")

    def test_to_int_with_thousands_sep(self):
        self.assertEqual(rt.to_int("1,200", "n", thousands_sep=","), 1200)

    def test_typ03_date_parses_declared_format_renders_iso(self):
        self.assertEqual(rt.to_date("01/02/2026", "dt", formats=["%m/%d/%Y"]),
                         "2026-01-02")

    def test_typ03_nonconforming_date_is_row_error(self):
        with self.assertRaises(rt.RowError) as ctx:
            rt.to_date("2026-13-99", "dt", formats=["%m/%d/%Y"])
        self.assertEqual(ctx.exception.code, "TYP-03")

    def test_typ04_naive_datetime_without_declared_tz_is_row_error(self):
        with self.assertRaises(rt.RowError) as ctx:
            rt.to_datetime("2026-01-02 03:04:05", "ts",
                           formats=["%Y-%m-%d %H:%M:%S"])
        self.assertEqual(ctx.exception.code, "TYP-04")

    def test_typ04_naive_datetime_with_declared_offset_converts_to_utc(self):
        v = rt.to_datetime("2026-01-02 05:34:05", "ts",
                           formats=["%Y-%m-%d %H:%M:%S"], assume_tz="+05:30")
        self.assertEqual(v, "2026-01-02T00:04:05+0000")

    def test_typ06_confirmed_vocabulary_maps(self):
        self.assertIs(rt.to_bool("Y", "active", mapping={"Y": True, "N": False}), True)

    def test_typ06_out_of_vocabulary_is_row_error(self):
        with self.assertRaises(rt.RowError) as ctx:
            rt.to_bool("maybe", "active", mapping={"Y": True, "N": False})
        self.assertEqual(ctx.exception.code, "TYP-06")

    def test_typ06_casefold_acceptance_is_counted(self):
        # ERR-04: the case-insensitive second chance is an auto-fix (TYP-10)
        # and must be counted, not silent.
        report = rt.RunReport()
        self.assertIs(rt.to_bool("y", "active", mapping={"Y": True, "N": False},
                                 report=report), True)
        self.assertEqual(report.warnings[("active", "TYP-10")], 1)

    def test_typ11_overlength_is_row_error_never_truncated(self):
        with self.assertRaises(rt.RowError) as ctx:
            rt.check_length("12345678901", "zip", max_length=10)
        self.assertEqual(ctx.exception.code, "TYP-11")

    def test_typ09_range_violation_is_row_error(self):
        with self.assertRaises(rt.RowError) as ctx:
            rt.check_range(250, "pct", min=0, max=100)
        self.assertEqual(ctx.exception.code, "TYP-09")

    def test_nul04_null_at_non_nullable_target_is_row_error(self):
        with self.assertRaises(rt.RowError) as ctx:
            rt.not_null(None, "id")
        self.assertEqual(ctx.exception.code, "NUL-04")

    def test_all_coercers_pass_none_through(self):
        self.assertIsNone(rt.to_decimal(None, "c"))
        self.assertIsNone(rt.to_int(None, "c"))
        self.assertIsNone(rt.to_date(None, "c", formats=["%Y-%m-%d"]))
        self.assertIsNone(rt.to_bool(None, "c", mapping={}))
        self.assertIsNone(rt.check_length(None, "c", max_length=1))
        self.assertIsNone(rt.check_range(None, "c"))


class TestEnc06RepairMojibake(unittest.TestCase):
    """ENC-06 repair — upstreamed from the iteration-3 agent candidate. Opt-in,
    signature-gated, never lossy."""

    def test_repairs_double_encoded_utf8_and_counts(self):
        report = rt.RunReport()
        self.assertEqual(rt.repair_mojibake("JosÃ© GarcÃ­a", "customer", report),
                         "José García")
        self.assertEqual(report.warnings[("customer", "ENC-06")], 1)

    def test_leaves_clean_accented_text_untouched_uncounted(self):
        report = rt.RunReport()
        for v in ("José", "Réné", "plain"):
            self.assertEqual(rt.repair_mojibake(v, "customer", report), v)
        self.assertEqual(sum(report.warnings.values()), 0)

    def test_never_lossy_on_unrepairable_signature(self):
        # carries a signature char but does not round-trip: return original
        v = "Ã" + "☃"  # snowman can't encode latin-1
        self.assertEqual(rt.repair_mojibake(v, "c", None), v)

    def test_none_passes_through(self):
        self.assertIsNone(rt.repair_mojibake(None, "c", None))


class TestStr06SkipIf(unittest.TestCase):
    def test_true_condition_raises_skiprow_with_code_and_reason(self):
        with self.assertRaises(rt.SkipRow) as ctx:
            rt.skip_if(None, True, code="STR-06", reason="footer/total row")
        self.assertEqual(ctx.exception.code, "STR-06")
        self.assertEqual(ctx.exception.reason, "footer/total row")

    def test_false_condition_returns_value_untouched(self):
        self.assertEqual(rt.skip_if("keep me", False), "keep me")


class TestConcat(unittest.TestCase):
    def test_joins_values_with_sep(self):
        self.assertEqual(rt.concat(["Ada", "Lovelace"], "full_name", sep=" "),
                         "Ada Lovelace")

    def test_sql_null_propagation_any_none_yields_none(self):
        # NUL-05 default: SQL semantics — any null operand yields null.
        self.assertIsNone(rt.concat(["Ada", None], "full_name", sep=" "))


class TestReadTextWithPolicy(unittest.TestCase):
    def _write(self, data: bytes) -> str:
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        self.addCleanup(os.unlink, path)
        return path

    def test_enc02_bom_stripped_and_counted(self):
        report = rt.RunReport()
        path = self._write(b"\xef\xbb\xbfid,name\n1,a\n")
        text = rt.read_text_with_policy(path, "utf-8", report)
        self.assertTrue(text.startswith("id,"))
        self.assertEqual(report.warnings[("<file>", "ENC-02")], 1)

    def test_str07_line_endings_normalized_and_counted(self):
        report = rt.RunReport()
        path = self._write(b"id\r\n1\r2\n")
        text = rt.read_text_with_policy(path, "utf-8", report)
        self.assertEqual(text, "id\n1\n2\n")
        self.assertEqual(report.warnings[("<file>", "STR-07")], 1)

    def test_enc01_undecodable_file_is_run_error_never_replacement_chars(self):
        report = rt.RunReport()
        path = self._write(b"caf\xe9,x\n")  # latin-1 byte in claimed utf-8
        with self.assertRaises(rt.RunError) as ctx:
            rt.read_text_with_policy(path, "utf-8", report)
        self.assertEqual(ctx.exception.code, "ENC-01")


def _passthrough_transform(columns):
    def transform_row(row, report):
        return {c: row[c] for c in columns}
    return transform_row


class TestRunPipeline(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.out_dir = os.path.join(self.dir.name, "out")

    def _input(self, text: str, name="in.csv") -> str:
        path = os.path.join(self.dir.name, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def _config(self, **overrides):
        cfg = {
            "name": "test_pipeline",
            "encoding": "utf-8",
            "delimiter": ",",
            "expected_columns": ["id", "name"],
            "output_columns": ["id", "name"],
            "policies": {},
        }
        cfg.update(overrides)
        return cfg

    def _read_json(self, fname):
        with open(os.path.join(self.out_dir, fname), encoding="utf-8") as f:
            return json.load(f)

    def test_clean_run_exit_0_and_all_three_report_granularities(self):
        path = self._input("id,name\n1,a\n2,b\n")
        res = rt.run_pipeline(input_path=path, out_dir=self.out_dir,
                              config=self._config(),
                              transform_row=_passthrough_transform(["id", "name"]))
        self.assertEqual(res.exit_code, 0)
        summary = self._read_json("summary.json")          # ERR-03 iii
        self.assertEqual(summary["rows_in"], 2)
        self.assertEqual(summary["rows_out"], 2)
        self.assertIn("errors_by_type", summary)           # ERR-03 ii
        self.assertIn("warnings_by_type", summary)
        self.assertTrue(os.path.exists(os.path.join(self.out_dir, "errors.jsonl")))  # ERR-03 i
        self.assertTrue(os.path.exists(os.path.join(self.out_dir, "manifest.json")))  # ERR-06

    def test_str02_ragged_row_quarantined_with_coded_record_and_raw_preserved(self):
        path = self._input("id,name\n1,a\n2\n3,c\n")
        res = rt.run_pipeline(input_path=path, out_dir=self.out_dir,
                              config=self._config(),
                              transform_row=_passthrough_transform(["id", "name"]))
        self.assertEqual(res.exit_code, 2)
        errors = [json.loads(l) for l in
                  open(os.path.join(self.out_dir, "errors.jsonl"), encoding="utf-8")]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error_code"], "STR-02")
        self.assertEqual(errors[0]["row_number"], 3)
        with open(os.path.join(self.out_dir, "quarantine.csv"), encoding="utf-8") as f:
            qrows = list(csv.reader(f))
        self.assertEqual(qrows[1][0], "3")
        self.assertEqual(json.loads(qrows[1][1]), ["2"])  # raw row preserved for reprocessing

    def test_str06_blank_rows_skipped_and_counted_rows_reconcile(self):
        path = self._input("id,name\n1,a\n,\n2,b\n")
        res = rt.run_pipeline(input_path=path, out_dir=self.out_dir,
                              config=self._config(),
                              transform_row=_passthrough_transform(["id", "name"]))
        summary = self._read_json("summary.json")
        skipped = summary["warnings_by_type"].get("STR-06:<row>", 0)
        self.assertEqual(summary["rows_in"],
                         summary["rows_out"] + summary["rows_quarantined"] + skipped)
        self.assertEqual(res.exit_code, 0)

    def test_skiprow_footer_counted_not_silent(self):
        path = self._input("id,name\n1,a\nTotal,1 row\n")

        def transform_row(row, report):
            if not row["id"].isdigit():
                raise rt.SkipRow("STR-06", "footer")
            return {"id": row["id"], "name": row["name"]}

        rt.run_pipeline(input_path=path, out_dir=self.out_dir,
                        config=self._config(), transform_row=transform_row)
        summary = self._read_json("summary.json")
        self.assertEqual(summary["warnings_by_type"].get("STR-06:<row>"), 1)

    def test_key03_unexpected_column_warns(self):
        path = self._input("id,name,extra\n1,a,x\n")
        rt.run_pipeline(input_path=path, out_dir=self.out_dir,
                        config=self._config(),
                        transform_row=_passthrough_transform(["id", "name"]))
        summary = self._read_json("summary.json")
        self.assertEqual(summary["warnings_by_type"].get("KEY-03:<file>"), 1)

    def test_nul03_per_column_sentinels_applied_from_policy(self):
        path = self._input("id,name\n1,N/A\n")
        cfg = self._config(policies={"sentinels": {"name": ["N/A"]}})
        rt.run_pipeline(input_path=path, out_dir=self.out_dir, config=cfg,
                        transform_row=_passthrough_transform(["id", "name"]))
        summary = self._read_json("summary.json")
        self.assertEqual(summary["warnings_by_type"].get("NUL-03:name"), 1)
        with open(os.path.join(self.out_dir, "output.csv"), encoding="utf-8") as f:
            self.assertNotIn("N/A", f.read())

    def test_err01_fail_fast_disposition_aborts_with_reports(self):
        path = self._input("id,name\n1\n2,b\n")
        cfg = self._config(policies={"error_disposition": "fail-fast"})
        res = rt.run_pipeline(input_path=path, out_dir=self.out_dir, config=cfg,
                              transform_row=_passthrough_transform(["id", "name"]))
        self.assertEqual(res.exit_code, 1)
        self.assertEqual(res.report.run_error["code"], "STR-02")
        self.assertFalse(os.path.exists(os.path.join(self.out_dir, "output.csv")))

    def test_err02_error_budget_converts_run_to_failure(self):
        rows = "\n".join(["1"] * 4)  # every row ragged
        path = self._input("id,name\n" + rows + "\n")
        cfg = self._config(policies={"error_budget": {"percent": 50, "min_rows": 2}})
        res = rt.run_pipeline(input_path=path, out_dir=self.out_dir, config=cfg,
                              transform_row=_passthrough_transform(["id", "name"]))
        self.assertEqual(res.exit_code, 1)
        self.assertEqual(res.report.run_error["code"], "ERR-02")
        self.assertFalse(os.path.exists(os.path.join(self.out_dir, "output.csv")))

    # ------------------------------------------------------------------
    # STR-05: exact duplicates — keep-and-report by default, drop is explicit
    # ------------------------------------------------------------------

    def test_str05_exact_duplicates_kept_and_reported_by_default(self):
        path = self._input("id,name\n1,a\n1,a\n2,b\n")
        res = rt.run_pipeline(input_path=path, out_dir=self.out_dir,
                              config=self._config(),
                              transform_row=_passthrough_transform(["id", "name"]))
        self.assertEqual(res.exit_code, 0)
        summary = self._read_json("summary.json")
        self.assertEqual(summary["rows_out"], 3)  # kept
        self.assertEqual(summary["warnings_by_type"].get("STR-05:<row>"), 1)  # reported
        self.assertEqual(summary["exact_duplicate_rows"],
                         [{"row_number": 3, "first_seen_row": 2}])

    def test_str05_drop_exact_is_explicit_and_counted(self):
        path = self._input("id,name\n1,a\n1,a\n2,b\n")
        cfg = self._config(policies={"duplicate_rows": "drop_exact"})
        res = rt.run_pipeline(input_path=path, out_dir=self.out_dir, config=cfg,
                              transform_row=_passthrough_transform(["id", "name"]))
        summary = self._read_json("summary.json")
        self.assertEqual(summary["rows_out"], 2)
        self.assertEqual(summary["warnings_by_type"].get("STR-05:<row>"), 1)
        # accounting still reconciles: dropped duplicate is visible, not vanished
        self.assertEqual(summary["rows_in"], summary["rows_out"]
                         + summary["rows_quarantined"] + 1)

    # ------------------------------------------------------------------
    # ERR-01 option (c): annotate disposition (Airbyte change-ledger shape,
    # taxonomy IDs as reasons). Content failures annotate; structural (STR-02)
    # and declared-non-nullable (NUL-04) failures still quarantine.
    # ------------------------------------------------------------------

    def _annotate_run(self, csv_text, fields=None):
        path = self._input(csv_text)
        cfg = self._config(expected_columns=["id", "n"], output_columns=["id", "n"],
                          policies={"error_disposition": "annotate"})

        def t_id(row, report):
            return rt.not_null(row["id"], "id")

        def t_n(row, report):
            return rt.to_int(row["n"], "n")

        return rt.run_pipeline(input_path=path, out_dir=self.out_dir, config=cfg,
                               field_transforms=fields or [("id", t_id), ("n", t_n)])

    def test_annotate_content_failure_loads_row_nulls_field_ledgers_change(self):
        res = self._annotate_run("id,n\n1,7\n2,abc\n")
        self.assertEqual(res.exit_code, 2)  # loaded, but investigate
        summary = self._read_json("summary.json")
        self.assertEqual(summary["rows_out"], 2)          # row LOADED
        self.assertEqual(summary["rows_quarantined"], 0)
        self.assertEqual(summary["rows_annotated"], 1)
        self.assertEqual(summary["annotations_by_type"], {"TYP-01:n": 1})
        changes = [json.loads(l) for l in
                   open(os.path.join(self.out_dir, "changes.jsonl"), encoding="utf-8")]
        self.assertEqual(changes, [{"row_number": 3, "changes": [
            {"field": "n", "change": "NULLED", "reason": "TYP-01"}]}])
        with open(os.path.join(self.out_dir, "output.csv"), encoding="utf-8") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[2], ["2", ""])              # field nulled in output

    def test_annotate_structural_failure_still_quarantines(self):
        res = self._annotate_run("id,n\n1,7\n2\n")        # ragged row
        summary = self._read_json("summary.json")
        self.assertEqual(summary["rows_out"], 1)
        self.assertEqual(summary["rows_quarantined"], 1)  # STR-02 not annotatable
        self.assertEqual(summary["errors_by_type"], {"STR-02:<row>": 1})
        self.assertEqual(res.exit_code, 2)

    def test_annotate_non_nullable_violation_still_quarantines(self):
        res = self._annotate_run("id,n\n,7\n")            # empty NOT NULL id
        summary = self._read_json("summary.json")
        self.assertEqual(summary["rows_out"], 0)
        self.assertEqual(summary["rows_quarantined"], 1)  # NUL-04 can't be nulled
        self.assertEqual(summary["errors_by_type"], {"NUL-04:id": 1})

    def test_annotate_without_field_transforms_fails_loud(self):
        path = self._input("id,name\n1,a\n")
        cfg = self._config(policies={"error_disposition": "annotate"})
        res = rt.run_pipeline(input_path=path, out_dir=self.out_dir, config=cfg,
                              transform_row=_passthrough_transform(["id", "name"]))
        self.assertEqual(res.exit_code, 1)
        self.assertEqual(res.report.run_error["code"], "ERR-01")

    def test_unknown_disposition_fails_loud_never_silently_falls_back(self):
        path = self._input("id,name\n1,a\n")
        cfg = self._config(policies={"error_disposition": "annotate-and-pray"})
        res = rt.run_pipeline(input_path=path, out_dir=self.out_dir, config=cfg,
                              transform_row=_passthrough_transform(["id", "name"]))
        self.assertEqual(res.exit_code, 1)
        self.assertEqual(res.report.run_error["code"], "ERR-01")

    # ------------------------------------------------------------------
    # ERR-03(i): per-row error records adopt DuckDB reject_errors fields
    # ------------------------------------------------------------------

    def test_error_records_carry_column_idx_and_csv_line(self):
        path = self._input("id,name\n1,a\n2\n")
        rt.run_pipeline(input_path=path, out_dir=self.out_dir,
                        config=self._config(),
                        transform_row=_passthrough_transform(["id", "name"]))
        rec = [json.loads(l) for l in
               open(os.path.join(self.out_dir, "errors.jsonl"), encoding="utf-8")][0]
        self.assertEqual(rec["csv_line"], "2")            # raw line, CSV-rendered
        self.assertIn("column_idx", rec)                  # None for row-level errors
        self.assertIsNone(rec["column_idx"])

    # ------------------------------------------------------------------
    # ERR-06: manifest carries spec provenance
    # ------------------------------------------------------------------

    def test_err06_manifest_has_spec_hash_and_versions(self):
        path = self._input("id,name\n1,a\n")
        cfg = self._config(spec_version="0.1", generator_version="etl-generator/0.1")
        rt.run_pipeline(input_path=path, out_dir=self.out_dir, config=cfg,
                        transform_row=_passthrough_transform(["id", "name"]))
        manifest = self._read_json("manifest.json")
        self.assertEqual(manifest["spec_version"], "0.1")
        self.assertEqual(manifest["generator_version"], "etl-generator/0.1")
        expected_sha = hashlib.sha256(
            json.dumps(cfg, sort_keys=True, separators=(",", ":"),
                       default=str).encode("utf-8")).hexdigest()
        self.assertEqual(manifest["spec_sha256"], expected_sha)
        self.assertEqual(manifest["taxonomy_version"], rt.TAXONOMY_VERSION)
        self.assertEqual(manifest["runtime_version"], rt.RUNTIME_VERSION)

    # ------------------------------------------------------------------
    # ERR-05: a failed run leaves no partial output — but always the report
    # ------------------------------------------------------------------

    def test_key02_missing_column_returns_exit_1_with_reports_no_output(self):
        path = self._input("id,wrong\n1,a\n")
        res = rt.run_pipeline(input_path=path, out_dir=self.out_dir,
                              config=self._config(),
                              transform_row=_passthrough_transform(["id", "name"]))
        self.assertEqual(res.exit_code, 1)
        self.assertEqual(res.report.run_error["code"], "KEY-02")
        self.assertFalse(os.path.exists(os.path.join(self.out_dir, "output.csv")))
        manifest = self._read_json("manifest.json")
        self.assertEqual(manifest["run_error"]["code"], "KEY-02")

    def test_enc01_bad_encoding_returns_exit_1_with_reports_no_output(self):
        path = os.path.join(self.dir.name, "latin.csv")
        with open(path, "wb") as f:
            f.write(b"id,name\n1,caf\xe9\n")
        res = rt.run_pipeline(input_path=path, out_dir=self.out_dir,
                              config=self._config(),
                              transform_row=_passthrough_transform(["id", "name"]))
        self.assertEqual(res.exit_code, 1)
        self.assertEqual(res.report.run_error["code"], "ENC-01")
        self.assertFalse(os.path.exists(os.path.join(self.out_dir, "output.csv")))
        summary = self._read_json("summary.json")
        self.assertEqual(summary["run_error"]["code"], "ENC-01")


if __name__ == "__main__":
    unittest.main()
