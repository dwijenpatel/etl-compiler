"""Tests for the deterministic spec compiler (scripts/compile_spec.py).

Two halves: the strict etlspec YAML-subset loader (stdlib-only, fail-loud on
anything outside the documented subset), and the spec -> pipeline.py emitter
(byte-deterministic, behavior-verified end-to-end).

Run:  python3 -m unittest discover -s tests -v
"""
import importlib.util
import os
import unittest

_SPEC = importlib.util.spec_from_file_location(
    "compile_spec",
    os.path.join(os.path.dirname(__file__), "..",
                 "skill", "etl-generator", "scripts", "compile_spec.py"))
assert _SPEC is not None and _SPEC.loader is not None
cs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cs)

REPO = os.path.join(os.path.dirname(__file__), "..")
VENDOR_SPEC = os.path.join(REPO, "evals", "inputs", "vendor_orders.etlspec.yaml")


class TestLoaderScalars(unittest.TestCase):
    def _load(self, text):
        return cs.load_etlspec(text)

    def test_scalar_resolution_rules(self):
        d = self._load("a: true\nb: false\nc: null\nd: ~\ne: 42\nf: 2.5\ng: hello\n")
        self.assertIs(d["a"], True)
        self.assertIs(d["b"], False)
        self.assertIsNone(d["c"])
        self.assertIsNone(d["d"])
        self.assertEqual(d["e"], 42)
        self.assertEqual(d["f"], 2.5)
        self.assertEqual(d["g"], "hello")

    def test_no_yaml11_norway_booleans(self):
        # Y/N/yes/no/on/off stay STRINGS in the etlspec subset — deliberately.
        d = self._load("a: Y\nb: N\nc: yes\nd: no\ne: on\nf: off\n")
        self.assertEqual([d[k] for k in "abcdef"], ["Y", "N", "yes", "no", "on", "off"])

    def test_quoted_strings(self):
        d = self._load('a: "hi there"\nb: \'single\'\nc: "with \\"esc\\""\n'
                       "d: 'it''s'\ne: \"1234\"\n")
        self.assertEqual(d["a"], "hi there")
        self.assertEqual(d["b"], "single")
        self.assertEqual(d["c"], 'with "esc"')
        self.assertEqual(d["d"], "it's")
        self.assertEqual(d["e"], "1234")  # quoted number stays string

    def test_comments_and_blank_lines_ignored(self):
        d = self._load("# header\na: 1\n\n  # indented comment\nb: 2  # trailing\n")
        self.assertEqual(d, {"a": 1, "b": 2})


class TestLoaderStructures(unittest.TestCase):
    def test_nested_block_maps(self):
        d = cs.load_etlspec("outer:\n  inner:\n    k: v\n  k2: 2\n")
        self.assertEqual(d, {"outer": {"inner": {"k": "v"}, "k2": 2}})

    def test_flow_map_and_seq(self):
        d = cs.load_etlspec('m: {value: NFC, provenance: default}\n'
                            's: [a, b, "c d"]\nempty_m: {}\nempty_s: []\n')
        self.assertEqual(d["m"], {"value": "NFC", "provenance": "default"})
        self.assertEqual(d["s"], ["a", "b", "c d"])
        self.assertEqual(d["empty_m"], {})
        self.assertEqual(d["empty_s"], [])

    def test_nested_flow_collections(self):
        d = cs.load_etlspec("e: {value: {percent: 25, min_rows: 2}, provenance: explicit}\n"
                            't: {op: to_date, formats: ["%m/%d/%Y"]}\n')
        self.assertEqual(d["e"]["value"], {"percent": 25, "min_rows": 2})
        self.assertEqual(d["t"]["formats"], ["%m/%d/%Y"])

    def test_block_seq_of_block_maps(self):
        d = cs.load_etlspec(
            "mappings:\n"
            "  - target: a\n"
            "    source: x\n"
            "    transforms: []\n"
            "  - target: b\n"
            "    source: y\n")
        self.assertEqual(d["mappings"],
                         [{"target": "a", "source": "x", "transforms": []},
                          {"target": "b", "source": "y"}])

    def test_block_seq_of_flow_maps_multiline(self):
        # Flow map continued across lines (bracket-balanced) — used by real specs.
        d = cs.load_etlspec(
            "decisions:\n"
            "  - {id: TYP-07, choice: string,\n"
            '     evidence: "uniform 8-digit values"}\n')
        self.assertEqual(d["decisions"][0]["id"], "TYP-07")
        self.assertEqual(d["decisions"][0]["evidence"], "uniform 8-digit values")

    def test_loads_the_real_vendor_spec(self):
        d = cs.load_etlspec(open(VENDOR_SPEC, encoding="utf-8").read())
        self.assertEqual(d["name"], "vendor_orders")
        self.assertEqual(d["source"]["dialect"]["delimiter"], ",")
        self.assertEqual(d["source"]["expected_columns"][2], "amt")
        self.assertEqual(len(d["target"]["columns"]), 6)
        self.assertEqual(d["target"]["columns"][2],
                         {"name": "amount", "type": "decimal", "scale": 2, "nullable": True})
        self.assertEqual(d["policies"]["error_budget"]["value"],
                         {"percent": 25, "min_rows": 2})
        amount = [m for m in d["mappings"] if m["target"] == "amount"][0]
        self.assertEqual(amount["transforms"][0]["op"], "to_decimal")
        is_active = [m for m in d["mappings"] if m["target"] == "is_active"][0]
        # Y/N keys stay strings; true/false values resolve to booleans
        self.assertEqual(is_active["transforms"][0]["mapping"], {"Y": True, "N": False})
        order_date = [m for m in d["mappings"] if m["target"] == "order_date"][0]
        self.assertEqual(order_date["sentinels"]["values"], ["N/A"])
        self.assertEqual(d["review_required"], [])


class TestLoaderFailsLoud(unittest.TestCase):
    def _err(self, text):
        with self.assertRaises(cs.SpecError) as ctx:
            cs.load_etlspec(text)
        return str(ctx.exception)

    def test_tab_indentation_rejected(self):
        self.assertIn("tab", self._err("a:\n\tb: 1\n").lower())

    def test_anchors_rejected(self):
        self.assertIn("anchor", self._err("a: &x 1\n").lower())

    def test_block_scalars_rejected(self):
        self.assertIn("block scalar", self._err("a: |\n  text\n").lower())

    def test_unterminated_flow_rejected(self):
        self._err("a: {k: v\n")

    def test_bare_colon_in_flow_value_rejected(self):
        # values containing ':' must be quoted in the subset
        self._err("a: {k: b:c}\n")

    def test_errors_carry_line_numbers(self):
        msg = self._err("a: 1\nb: &anchor 2\n")
        self.assertIn("line 2", msg)


def _compile_vendor():
    text = open(VENDOR_SPEC, encoding="utf-8").read()
    spec = cs.load_etlspec(text)
    return cs.compile_spec(spec, spec_bytes=text.encode("utf-8"),
                           spec_filename="vendor_orders.etlspec.yaml")


class TestCompilerDeterminism(unittest.TestCase):
    def test_compile_twice_is_byte_identical(self):
        self.assertEqual(_compile_vendor(), _compile_vendor())

    def test_header_carries_spec_hash_and_compiler_version_no_timestamp(self):
        import hashlib
        code = _compile_vendor()
        sha = hashlib.sha256(open(VENDOR_SPEC, "rb").read()).hexdigest()
        self.assertIn(sha, code)
        self.assertIn(cs.COMPILER_VERSION, code)
        self.assertNotIn("2026-", code)  # no wall-clock anything

    def test_generated_code_is_valid_python(self):
        compile(_compile_vendor(), "vendor_orders_pipeline.py", "exec")


class TestCompilerEndToEnd(unittest.TestCase):
    def test_compiled_pipeline_runs_and_matches_known_behavior(self):
        import csv as csvmod
        import json
        import shutil
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "vendor_orders_pipeline.py"), "w") as f:
                f.write(_compile_vendor())
            for rt_file in ("etl_runtime.py", "etl_coercers.py"):  # two-file runtime
                shutil.copy(os.path.join(REPO, "skill", "etl-generator", "assets",
                                         rt_file), tmp)
            sample = os.path.join(REPO, "evals", "inputs", "vendor_orders_sample.csv")
            proc = subprocess.run(
                ["python3", "vendor_orders_pipeline.py", sample, "--out-dir", "out"],
                cwd=tmp, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(os.path.join(tmp, "out", "output.csv")) as f:
                rows = list(csvmod.reader(f))
            self.assertEqual(rows[0], ["order_id", "customer_name", "amount",
                                       "order_date", "is_active", "postal_code"])
            self.assertEqual(rows[1], ["20000001", "Alice Wong", "2450.00",
                                       "2026-01-15", "true", "02134"])
            self.assertEqual(rows[2][2], "-75.00")          # (75) accounting negative
            self.assertEqual(rows[3][3], "")                # N/A sentinel -> null
            self.assertEqual(rows[4][2], "1000.00")         # $1,000 at scale 2
            summary = json.load(open(os.path.join(tmp, "out", "summary.json")))
            self.assertEqual(summary["rows_in"], 4)
            self.assertEqual(summary["rows_out"], 4)
            self.assertEqual(summary["warnings_by_type"].get("NUL-03:dt"), 1)
            manifest = json.load(open(os.path.join(tmp, "out", "manifest.json")))
            self.assertEqual(manifest["generator_version"],
                             f"etl-spec-compiler/{cs.COMPILER_VERSION}")


MINI_SPEC = """etlspec: 0.2
name: mini
taxonomy_version: 0.2
source:
  format: csv
  encoding: {value: utf-8, provenance: default}
  dialect: {delimiter: ",", quotechar: '"'}
  header: present
  expected_columns: [id, name, note]
target:
  columns:
    - {name: id, type: string, nullable: false}
    - {name: name, type: string, nullable: true}
    - {name: note, type: string, nullable: true}
policies:
  unicode_normalization: {value: NFC, provenance: default}
  strip_control_chars: {value: true, provenance: default}
  normalize_unicode_whitespace: {value: true, provenance: default}
  trim_whitespace: {value: true, provenance: default}
  empty_string_is_null: {value: true, provenance: default}
  null_propagation: {value: sql, provenance: default}
  datetime_rendering: {value: iso8601, provenance: default}
  error_disposition: {value: quarantine, provenance: default}
  error_budget: {value: {percent: 50, min_rows: 2}, provenance: explicit}
  duplicate_rows: {value: keep, provenance: default}
skip_rows:
  - {column: id, pattern: '^\\D', id: STR-06, reason: "footer/total row", provenance: explicit}
mappings:
  - target: id
    source: id
    transforms: []
  - target: name
    source: name
    transforms:
      - {op: repair_mojibake}
    decisions:
      - {id: ENC-06, choice: repair, provenance: explicit}
  - target: note
    source: note
    transforms:
      - {op: expr, python: "(row['note'] or '').upper() if report else None"}
unmapped:
  unused_source_columns: []
  unfilled_target_columns: []
review_required: []
"""


def _compile_mini():
    spec = cs.load_etlspec(MINI_SPEC)
    return cs.compile_spec(spec, spec_bytes=MINI_SPEC.encode(), spec_filename="mini.etlspec.yaml")


class TestCompilerNewOps(unittest.TestCase):
    def _run_mini(self, csv_text):
        import shutil
        import subprocess
        import tempfile
        import json as jsonmod
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "mini_pipeline.py"), "w") as f:
            f.write(_compile_mini())
        for rt_file in ("etl_runtime.py", "etl_coercers.py"):  # two-file runtime
            shutil.copy(os.path.join(REPO, "skill", "etl-generator", "assets",
                                     rt_file), tmp)
        with open(os.path.join(tmp, "in.csv"), "w", encoding="utf-8", newline="") as f:
            f.write(csv_text)
        p = subprocess.run(["python3", "mini_pipeline.py", "in.csv", "--out-dir", "out"],
                           cwd=tmp, capture_output=True, text=True)
        summary = jsonmod.load(open(os.path.join(tmp, "out", "summary.json")))
        return p, summary, tmp

    def test_spec_format_02_accepted(self):
        self.assertIn("repair_mojibake", _compile_mini())

    def test_repair_mojibake_op_emitted_with_report_and_counted(self):
        code = _compile_mini()
        self.assertIn("rt.repair_mojibake(", code)
        self.assertIn("report=report", code)
        p, summary, _ = self._run_mini("id,name,note\n1,JosÃ© GarcÃ­a,x\n2,ok,y\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(summary["warnings_by_type"].get("ENC-06:name"), 1)

    def test_skip_rows_declarative_footer_skipped_and_counted(self):
        p, summary, tmp = self._run_mini("id,name,note\n1,a,x\nTotal,,\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(summary["rows_out"], 1)
        self.assertEqual(summary["warnings_by_type"].get("STR-06:<row>"), 1)

    def test_expr_helper_receives_report(self):
        # the mini spec's expr references `report`; it must compile AND run.
        p, summary, tmp = self._run_mini("id,name,note\n1,a,hello\n")
        import csv as csvmod
        rows = list(csvmod.reader(open(os.path.join(tmp, "out", "output.csv"))))
        self.assertEqual(rows[1][2], "HELLO")

    def test_concat_op_supported(self):
        spec = cs.load_etlspec(MINI_SPEC.replace(
            "- {op: repair_mojibake}",
            "- {op: concat, sources: [id, name], sep: \"-\"}"))
        code = cs.compile_spec(spec, spec_bytes=b"x", spec_filename="t.yaml")
        self.assertIn("rt.concat(", code)

    def test_split_op_declined_with_guidance(self):
        spec = cs.load_etlspec(MINI_SPEC.replace(
            "- {op: repair_mojibake}",
            "- {op: split, on: \"-\", index: 0}"))
        with self.assertRaises(cs.SpecError) as ctx:
            cs.compile_spec(spec, spec_bytes=b"x", spec_filename="t.yaml")
        self.assertIn("split", str(ctx.exception))


class TestAnnotateDisposition(unittest.TestCase):
    def test_unknown_disposition_value_rejected_at_compile_time(self):
        bad = MINI_SPEC.replace("error_disposition: {value: quarantine, provenance: default}",
                                "error_disposition: {value: sideline, provenance: default}")
        with self.assertRaises(cs.SpecError) as ctx:
            cs.validate_spec(cs.load_etlspec(bad))
        self.assertIn("error_disposition", str(ctx.exception))

    def test_compiled_pipeline_wires_field_transforms_and_guards(self):
        code = _compile_mini()
        self.assertIn("FIELD_TRANSFORMS = [", code)
        self.assertIn("field_transforms=FIELD_TRANSFORMS", code)
        self.assertIn("row_guards=_row_guards", code)  # mini spec has skip_rows

    def test_compiled_annotate_content_failure_ledgered(self):
        import json as jsonmod
        import shutil
        import subprocess
        import tempfile
        # nullable integer column 'note' -> to_int; bad value must NULL+ledger.
        spec_text = MINI_SPEC.replace(
            "error_disposition: {value: quarantine, provenance: default}",
            "error_disposition: {value: annotate, provenance: explicit}").replace(
            "etlspec: 0.2", "etlspec: 0.3").replace(
            "- {name: note, type: string, nullable: true}",
            "- {name: note, type: integer, nullable: true}").replace(
            "  - target: note\n    source: note\n    transforms:\n"
            "      - {op: expr, python: \"(row['note'] or '').upper() if report else None\"}",
            "  - target: note\n    source: note\n    transforms:\n      - {op: to_int}")
        spec = cs.load_etlspec(spec_text)
        code = cs.compile_spec(spec, spec_bytes=spec_text.encode(), spec_filename="t.yaml")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "p.py"), "w") as f:
            f.write(code)
        for rt_file in ("etl_runtime.py", "etl_coercers.py"):  # two-file runtime
            shutil.copy(os.path.join(REPO, "skill", "etl-generator", "assets",
                                     rt_file), tmp)
        with open(os.path.join(tmp, "in.csv"), "w", newline="") as f:
            f.write("id,name,note\n1,a,7\n2,b,seven\n")
        p = subprocess.run(["python3", "p.py", "in.csv", "--out-dir", "out"],
                           cwd=tmp, capture_output=True, text=True)
        self.assertEqual(p.returncode, 2, p.stderr)
        s = jsonmod.load(open(os.path.join(tmp, "out", "summary.json")))
        self.assertEqual((s["rows_out"], s["rows_quarantined"], s["rows_annotated"]),
                         (2, 0, 1))
        self.assertEqual(s["annotations_by_type"], {"TYP-01:note": 1})
        changes = [jsonmod.loads(l) for l in
                   open(os.path.join(tmp, "out", "changes.jsonl"))]
        self.assertEqual(changes[0]["changes"],
                         [{"field": "note", "change": "NULLED", "reason": "TYP-01"}])


class TestFullMessySpecEndToEnd(unittest.TestCase):
    """The iteration-3 gap, closed: the 17-trap orders_export file is now fully
    compiler-expressible (ENC-06 repair + STR-06 skip_rows as first-class ops)."""

    def test_orders_spec_compiles_and_reproduces_known_accounting(self):
        import json as jsonmod
        import shutil
        import subprocess
        import tempfile
        spec_path = os.path.join(REPO, "evals", "inputs", "orders_export.etlspec.yaml")
        text = open(spec_path, encoding="utf-8").read()
        spec = cs.load_etlspec(text)
        code = cs.compile_spec(spec, spec_bytes=text.encode(),
                               spec_filename="orders_export.etlspec.yaml")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "orders_pipeline.py"), "w") as f:
            f.write(code)
        for rt_file in ("etl_runtime.py", "etl_coercers.py"):  # two-file runtime
            shutil.copy(os.path.join(REPO, "skill", "etl-generator", "assets",
                                     rt_file), tmp)
        p = subprocess.run(
            ["python3", "orders_pipeline.py",
             os.path.join(REPO, "evals", "inputs", "orders_export.csv"),
             "--out-dir", "out"],
            cwd=tmp, capture_output=True, text=True)
        self.assertEqual(p.returncode, 2, p.stderr)  # completed with quarantine
        s = jsonmod.load(open(os.path.join(tmp, "out", "summary.json")))
        self.assertEqual((s["rows_in"], s["rows_out"], s["rows_quarantined"]), (9, 6, 1))
        w = s["warnings_by_type"]
        self.assertEqual(w.get("ENC-06:customer"), 1)   # mojibake repaired + counted
        self.assertEqual(w.get("STR-06:<row>"), 2)      # blank row + footer
        self.assertEqual(w.get("STR-05:<row>"), 1)      # duplicate kept + reported
        self.assertEqual(s["errors_by_type"], {"STR-02:<row>": 1})  # ragged quarantined
        import csv as csvmod
        rows = list(csvmod.reader(open(os.path.join(tmp, "out", "output.csv"))))
        self.assertEqual(len(rows) - 1, 6)
        self.assertEqual(rows[1][1], "José")            # repaired + NFC
        self.assertIn("02134", rows[1][5])              # leading zero preserved


class TestCompilerInjectionAndFailLoud(unittest.TestCase):
    """A spec is the attack surface. Code may reach the pipeline ONLY through the
    loud `op: expr` hatch — never smuggled through metadata into docstring/comments.
    And malformed specs fail loud (SpecError + line), never raw KeyError/traceback."""

    def _compiles_without_injection(self, text, marker="INJECTED"):
        spec = cs.load_etlspec(text)
        code = cs.compile_spec(spec, spec_bytes=text.encode(), spec_filename="t.yaml")
        # the emitted module must import + define transform_row without executing
        # any smuggled top-level statement
        ns = {}
        import types
        mod = types.ModuleType("emitted")
        # stub the runtime import so exec doesn't need the real module on path
        import sys
        sys.modules.setdefault("etl_runtime", types.ModuleType("etl_runtime"))
        exec(compile(code, "emitted", "exec"), mod.__dict__)
        return code

    def test_taxonomy_version_cannot_break_out_of_docstring(self):
        bad = MINI_SPEC.replace("taxonomy_version: 0.2",
                                'taxonomy_version: "0.2\\"\\"\\"\\nprint(1/0)\\n\\"\\"\\""')
        with self.assertRaises(cs.SpecError):
            cs.validate_spec(cs.load_etlspec(bad))

    def test_decision_id_newline_cannot_inject_into_comment(self):
        bad = MINI_SPEC.replace(
            "decisions:\n      - {id: ENC-06, choice: repair, provenance: explicit}",
            'decisions:\n      - {id: "ENC-06\\nprint(1/0)  ", choice: x, provenance: explicit}')
        # either rejected, or emitted such that no injected statement executes
        try:
            code = self._compiles_without_injection(bad)
            self.assertNotIn("\nprint(1/0)", code)
        except cs.SpecError:
            pass

    def test_expr_without_python_fails_loud(self):
        bad = MINI_SPEC.replace("- {op: repair_mojibake}", "- {op: expr}")
        with self.assertRaises(cs.SpecError) as ctx:
            cs.compile_spec(cs.load_etlspec(bad), spec_bytes=b"x", spec_filename="t.yaml")
        self.assertIn("expr", str(ctx.exception).lower())

    def test_constant_without_value_fails_loud(self):
        bad = MINI_SPEC.replace("- {op: repair_mojibake}", "- {op: constant}")
        with self.assertRaises(cs.SpecError):
            cs.compile_spec(cs.load_etlspec(bad), spec_bytes=b"x", spec_filename="t.yaml")

    def test_sentinels_missing_provenance_fails_loud(self):
        bad = MINI_SPEC.replace(
            "  - target: note\n    source: note",
            "  - target: note\n    source: note\n    sentinels: {values: [\"N/A\"]}")
        with self.assertRaises(cs.SpecError):
            cs.compile_spec(cs.load_etlspec(bad), spec_bytes=b"x", spec_filename="t.yaml")

    def test_decision_provenance_validated(self):
        bad = MINI_SPEC.replace("provenance: explicit}\n  - target: note",
                                "provenance: made-it-up}\n  - target: note")
        with self.assertRaises(cs.SpecError):
            cs.compile_spec(cs.load_etlspec(bad), spec_bytes=b"x", spec_filename="t.yaml")

    def test_unfilled_column_name_cannot_inject_into_comment(self):
        # A newline smuggled into a column name is now REJECTED at load time
        # (defense by construction), not merely sanitized at emission.
        bad = MINI_SPEC.replace(
            "- {name: note, type: string, nullable: true}",
            '- {name: "note\\n    import os", type: string, nullable: true}').replace(
            "  - target: note\n    source: note\n    transforms:\n"
            "      - {op: expr, python: \"(row['note'] or '').upper() if report else None\"}",
            "").replace(
            "unfilled_target_columns: []",
            'unfilled_target_columns: ["note\\n    import os"]')
        with self.assertRaises(cs.SpecError) as ctx:
            cs.compile_spec(cs.load_etlspec(bad), spec_bytes=b"x", spec_filename="t.yaml")
        self.assertIn("control character", str(ctx.exception))

    def test_name_with_trailing_newline_rejected(self):
        bad = MINI_SPEC.replace("name: mini", 'name: "mini\\n"')
        with self.assertRaises(cs.SpecError):
            cs.validate_spec(cs.load_etlspec(bad))


class TestCompilerValidation(unittest.TestCase):
    def _spec(self, **override):
        text = open(VENDOR_SPEC, encoding="utf-8").read()
        spec = cs.load_etlspec(text)
        spec.update(override)
        return spec

    def _err(self, spec):
        with self.assertRaises(cs.SpecError) as ctx:
            cs.compile_spec(spec, spec_bytes=b"x", spec_filename="t.yaml")
        return str(ctx.exception)

    def test_missing_policy_key_fails_naming_it(self):
        spec = self._spec()
        del spec["policies"]["duplicate_rows"]
        self.assertIn("duplicate_rows", self._err(spec))

    def test_unknown_transform_op_fails(self):
        spec = self._spec()
        spec["mappings"][2]["transforms"] = [{"op": "to_magic"}]
        self.assertIn("to_magic", self._err(spec))

    def test_mapping_to_unknown_target_column_fails(self):
        spec = self._spec()
        spec["mappings"][0]["target"] = "nonexistent"
        self.assertIn("nonexistent", self._err(spec))

    def test_unfilled_target_column_must_be_declared(self):
        spec = self._spec()
        spec["mappings"] = spec["mappings"][1:]  # drop order_id mapping
        msg = self._err(spec)
        self.assertIn("order_id", msg)

    def test_absent_header_not_supported_fails_loud(self):
        spec = self._spec()
        spec["source"]["header"] = "absent"
        self.assertIn("header", self._err(spec).lower())


class TestFormulaInjectionPolicy(unittest.TestCase):
    """ENC-08 / spec format 0.4: the formula_injection policy key — required
    from 0.4, validated-and-emitted whenever present, defaulted (pass-through,
    the taxonomy house default) for the pre-0.4 formats that predate it."""

    POLICY = "  formula_injection: {value: neutralize, provenance: explicit}\n"
    ANCHOR = "  duplicate_rows: {value: keep, provenance: default}\n"

    def _spec_text(self, version, policy_line=""):
        return (MINI_SPEC.replace("etlspec: 0.2", f"etlspec: {version}")
                .replace(self.ANCHOR, self.ANCHOR + policy_line))

    def _compile(self, text):
        spec = cs.load_etlspec(text)
        return cs.compile_spec(spec, spec_bytes=text.encode(),
                               spec_filename="mini.etlspec.yaml")

    def test_pre_04_spec_without_key_still_compiles(self):
        code = self._compile(self._spec_text("0.2"))
        self.assertNotIn("formula_injection", code)  # runtime default applies

    def test_04_spec_without_key_fails_naming_it(self):
        with self.assertRaises(cs.SpecError) as ctx:
            self._compile(self._spec_text("0.4"))
        self.assertIn("policies.formula_injection", str(ctx.exception))

    def test_04_key_emitted_into_config(self):
        code = self._compile(self._spec_text("0.4", self.POLICY))
        self.assertIn("'formula_injection': 'neutralize'", code)

    def test_unknown_value_rejected_with_decision_space(self):
        bad = "  formula_injection: {value: strip, provenance: explicit}\n"
        with self.assertRaises(cs.SpecError) as ctx:
            self._compile(self._spec_text("0.4", bad))
        self.assertIn("ENC-08", str(ctx.exception))

    def test_pre_04_spec_declaring_key_is_validated_and_emitted_not_dropped(self):
        code = self._compile(self._spec_text("0.2", self.POLICY))
        self.assertIn("'formula_injection': 'neutralize'", code)
        bad = "  formula_injection: {value: strip, provenance: explicit}\n"
        with self.assertRaises(cs.SpecError):
            self._compile(self._spec_text("0.2", bad))

    def test_compiled_neutralize_pipeline_prefixes_output_end_to_end(self):
        import csv as csvmod
        import json as jsonmod
        import shutil
        import subprocess
        import tempfile
        code = self._compile(self._spec_text("0.4", self.POLICY))
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "mini_pipeline.py"), "w") as f:
            f.write(code)
        for rt_file in ("etl_runtime.py", "etl_coercers.py"):  # two-file runtime
            shutil.copy(os.path.join(REPO, "skill", "etl-generator", "assets",
                                     rt_file), tmp)
        with open(os.path.join(tmp, "in.csv"), "w", encoding="utf-8", newline="") as f:
            f.write("id,name,note\n1,=2+2,x\n2,-500,y\n")
        p = subprocess.run(["python3", "mini_pipeline.py", "in.csv", "--out-dir", "out"],
                           cwd=tmp, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = list(csvmod.reader(open(os.path.join(tmp, "out", "output.csv"))))
        self.assertEqual(rows[1][1], "'=2+2")   # neutralized, counted
        self.assertEqual(rows[2][1], "-500")    # plain negative untouched
        summary = jsonmod.load(open(os.path.join(tmp, "out", "summary.json")))
        self.assertEqual(summary["warnings_by_type"].get("ENC-08:name"), 1)


if __name__ == "__main__":
    unittest.main()
