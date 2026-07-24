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
assert _SPEC is not None and _SPEC.loader is not None
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

    def test_typ12_magnitude_suffix_numeric_detected(self):
        # NOAA storm events store damage as "10.00K", "1.20M" — its own entry TYP-12.
        text = "event,damage\na,10.00K\nb,1.20M\nc,0.00K\nd,250.00K\n"
        self.assertIn("TYP-12", _ids(_profile_text(text)))


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


class TestCodeReviewFixes(unittest.TestCase):
    """Fixes from the 2026-07-21 profiler review — verified crash/miss cases."""

    def test_mixed_separator_dates_do_not_crash(self):
        # F1: a column mixing / and . separators must not raise ValueError.
        text = "when,n\n01/02/2020,1\n03.04.2020,2\n05/06/2020,3\n07/08/2020,4\n"
        result = _profile_text(text)  # must not raise
        self.assertIsInstance(result["findings"], list)

    def test_distinct_same_id_questions_do_not_collapse(self):
        # F2: duplicate-header STR-04 and blank-header STR-04 are DIFFERENT
        # decisions; both must survive into interview_groups (never guess).
        text = "id,id,\n1,2,3\n4,5,6\n"
        groups = _profile_text(text)["interview_groups"]
        str04 = [g for g in groups if g["id"] == "STR-04"]
        msgs = {g["message"] for g in str04}
        self.assertTrue(any("duplicate" in m for m in msgs), msgs)
        self.assertTrue(any("blank" in m for m in msgs), msgs)

    def test_single_column_file_no_spurious_delimiter_question(self):
        # F8: a legitimate single-column file must not raise an STR-01 "confirm
        # delimiter" ask.
        ids = _ids(_profile_text("name\nAlice\nBob\nCarol\n"))
        self.assertNotIn("STR-01", ids)

    def test_blank_rows_not_counted_as_exact_duplicates(self):
        # F6: identical blank rows are STR-06 (blank), not STR-05 (duplicate) —
        # consistency with the runtime, which skips blanks before the dup check.
        result = _profile_text("id,n\n1,a\n\n\n\n")
        ids = _ids(result)
        self.assertNotIn("STR-05", ids)

    def test_preamble_with_blank_separator_line_finds_real_header(self):
        # F3: ONS shape with a blank line between metadata block and data.
        text = ('"Title","GDP"\n"CDID","IHYQ"\n"Unit","%"\n'
                '\n'
                '"1955 Q1","0.1"\n"1955 Q2","1.7"\n"1955 Q3","-0.6"\n"1955 Q4","0.2"\n')
        result = _profile_text(text)
        ids = _ids(result)
        self.assertIn("STR-06", ids)
        # header must NOT be the empty/blank row, and data rows must not all be
        # mis-reported as ragged
        self.assertNotEqual(result["columns"], [])
        self.assertNotIn("STR-02", ids)


class TestInterviewGrouping(unittest.TestCase):
    """Homogeneous per-column ask-findings must collapse into one grouped question
    so the interview scales (corpus: a 9,074-column file produced 9,073 TYP-06 Qs)."""

    def test_same_boolean_vocab_columns_group_into_one_question(self):
        cols = ",".join(f"flag{i}" for i in range(5))
        rows = "\n".join(",".join(["Y", "N"][i % 2] for _ in range(5)) for i in range(6))
        result = _profile_text(cols + "\n" + rows + "\n")
        groups = result["interview_groups"]
        typ06 = [g for g in groups if g["id"] == "TYP-06"]
        self.assertEqual(len(typ06), 1)
        self.assertEqual(typ06[0]["n_columns"], 5)
        self.assertEqual(sorted(typ06[0]["columns"]), [f"flag{i}" for i in range(5)])

    def test_different_boolean_vocabs_stay_separate_groups(self):
        # Y/N columns and 0/1 columns are different decisions — two groups.
        # Each column must carry both of its values (a constant column isn't boolean).
        rows = ["Y,N,1,0", "N,Y,0,1"] * 3
        text = "yn1,yn2,tf1,tf2\n" + "\n".join(rows) + "\n"
        groups = _profile_text(text)["interview_groups"]
        typ06 = [g for g in groups if g["id"] == "TYP-06"]
        self.assertEqual(len(typ06), 2)

    def test_grouping_preserves_total_column_coverage(self):
        # No column silently dropped from the interview plan.
        cols = ",".join(f"z{i}" for i in range(8))
        rows = "\n".join(",".join("0" + str(i) for i in range(8)) for _ in range(5))
        result = _profile_text(cols + "\n" + rows + "\n")
        t07_cols = set()
        for g in result["interview_groups"]:
            if g["id"] == "TYP-07":
                t07_cols |= set(g["columns"])
        t07_flat = {f["column"] for f in result["findings"]
                    if f["id"] == "TYP-07" and f.get("column")}
        self.assertEqual(t07_cols, t07_flat)


if __name__ == "__main__":
    unittest.main()
