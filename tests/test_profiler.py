"""Unit tests for the profiler (scripts/profile.py) — corpus-audit-driven regressions.

Run:  python3 -m unittest discover -s tests -v
"""
import importlib.util
import os
import sys
import tempfile
import unittest

_SPEC = importlib.util.spec_from_file_location(
    "profile_mod",
    os.path.join(os.path.dirname(__file__), "..",
                 "skill", "etl-generator", "scripts", "profile.py"))
prof = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(prof)


def _profile_text(text: str, **kw) -> dict:
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    try:
        return prof.profile_file(path, **kw)
    finally:
        os.unlink(path)


def _ids(result):
    return [f["id"] for f in result["findings"]]


class TestProfilerRobustness(unittest.TestCase):
    def test_empty_file_does_not_crash(self):
        # corpus regression: frictionless data__empty.csv crashed profile_file
        result = _profile_text("")
        self.assertEqual(result["rows_profiled"], 0)
        self.assertEqual(result["columns"], [])

    def test_header_only_file_does_not_crash(self):
        result = _profile_text("id,name\n")
        self.assertEqual(result["rows_profiled"], 0)


class TestCorpusDetectionGaps(unittest.TestCase):
    """Regressions from the stream-A corpus audit (portal files). Each pins a
    real failure mode the profiler missed on a genuine open-data file."""

    def test_str06_preamble_rows_above_header_detected(self):
        # Real ONS time-series shape: key-value metadata rows, no header, then a
        # numeric second column. Profiler previously read row 0 ("Title","...")
        # as the header and missed the metadata block entirely.
        preamble = ('"Title","GDP q-on-q growth"\n"CDID","IHYQ"\n'
                    '"Source dataset ID","PN2"\n"Unit","%"\n"Release date","14-05-2026"\n')
        data = "".join(f'"{1990 + i} Q1","{i * 0.1 - 0.5:.1f}"\n' for i in range(12))
        ids = _ids(_profile_text(preamble + data))
        self.assertIn("STR-06", ids)

    def test_typ03_dotted_european_dates_detected(self):
        # DD.MM.YYYY (dot separator) — the dominant European date format, and a
        # silent-corruption risk. DATE_SLASH_RE only matched / and - before.
        text = "when,n\n08.08.2018,1\n09.08.2018,2\n10.08.2018,3\n13.08.2018,4\n"
        self.assertIn("TYP-03", _ids(_profile_text(text)))

    def test_nul03_parenthesized_null_sentinel_detected(self):
        # NYPD complaint data uses the literal string "(null)" as a sentinel.
        rows = "".join(f"id{i},(null)\n" for i in range(20))
        text = "id,val\n" + rows
        self.assertIn("NUL-03", _ids(_profile_text(text)))

    def test_enc02_doubled_bom_fully_stripped(self):
        # Leicester licensing file carried two UTF-8 BOMs; one survived into the
        # first header name. Every leading BOM must be stripped.
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "wb") as f:
            f.write("﻿﻿Licence Number;Make\n1;Ford\n2;Vauxhall\n"
                    .encode("utf-8"))
        try:
            result = prof.profile_file(path)
        finally:
            os.unlink(path)
        self.assertNotIn("﻿", result["columns"][0])
        self.assertEqual(result["columns"][0], "Licence Number")

    def test_typ01_magnitude_suffix_numeric_detected(self):
        # NOAA storm events store damage as "10.00K", "1.20M" — magnitude
        # suffixes TYP-01 did not recognize.
        text = "event,damage\na,10.00K\nb,1.20M\nc,0.00K\nd,250.00K\n"
        self.assertIn("TYP-01", _ids(_profile_text(text)))


class TestTyp06Boolean(unittest.TestCase):
    def test_single_distinct_value_does_not_fire(self):
        # corpus regression: 9,073 spurious TYP-06 findings on a 9,074-column
        # file whose columns each hold one repeated value ("1", "x", ...).
        # One distinct value is not evidence of a boolean vocabulary.
        result = _profile_text("a,b\n1,x\n1,x\n1,x\n")
        self.assertNotIn("TYP-06", _ids(result))

    def test_yn_column_fires(self):
        result = _profile_text("id,active\n1,Y\n2,N\n3,Y\n")
        self.assertIn("TYP-06", _ids(result))

    def test_zero_one_column_fires(self):
        result = _profile_text("id,flag\n1,0\n2,1\n3,1\n")
        self.assertIn("TYP-06", _ids(result))


if __name__ == "__main__":
    unittest.main()
