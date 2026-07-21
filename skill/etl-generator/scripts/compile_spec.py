#!/usr/bin/env python3
"""compile_spec.py — deterministic .etlspec.yaml → pipeline.py compiler.

Same spec bytes → byte-identical pipeline, no model in the loop. Stdlib only.

Because the standard library has no YAML parser and this skill promises
zero dependencies, this file includes a STRICT loader for the documented
etlspec subset of YAML (see references/spec-format.md):

  - block mappings and block sequences (2-space indentation, no tabs)
  - flow mappings {k: v, ...} and flow sequences [a, b] — nesting allowed,
    and a flow collection may continue across lines until brackets balance
  - scalars: single-/double-quoted strings, and bare words where ONLY
    `true`/`false`, `null`/`~`, integers, and floats resolve to non-strings.
    Deliberately NOT YAML 1.1: `Y`/`N`/`yes`/`no`/`on`/`off` stay strings
    (no Norway problem). Values containing `:`  `#`  `{}[],` must be quoted.
  - comments (# ...) and blank lines

Anything outside the subset (tabs, anchors/aliases, tags, block scalars,
multi-doc, unquoted colons in flow) is a hard SpecError with a line number —
this compiler never guesses what a spec means.

Usage: python3 compile_spec.py SPEC.etlspec.yaml [-o OUT.py]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys

COMPILER_VERSION = "0.1.0"


class SpecError(Exception):
    """A spec the compiler refuses to interpret. Message carries the line."""


# =====================================================================
# Loader — strict etlspec subset of YAML
# =====================================================================

_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")
_FLOW_STOP = set(",}]:")


def _fail(lineno, msg):
    raise SpecError(f"line {lineno}: {msg}")


def _strip_comment(line, lineno):
    """Remove a trailing comment, respecting quotes. Returns stripped line."""
    out = []
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if quote == "'" and ch == "'":
                if i + 1 < len(line) and line[i + 1] == "'":
                    out.append("'")
                    i += 1
                else:
                    quote = None
            elif quote == '"':
                if ch == "\\" and i + 1 < len(line):
                    out.append(line[i + 1])
                    i += 1
                elif ch == '"':
                    quote = None
        elif ch in "'\"":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
        i += 1
    if quote:
        _fail(lineno, "unterminated quoted string")
    return "".join(out).rstrip()


def _bracket_delta(s, lineno):
    """Net {[ vs ]} depth outside quotes."""
    depth = 0
    quote = None
    i = 0
    while i < len(s):
        ch = s[i]
        if quote:
            if quote == "'" and ch == "'":
                if i + 1 < len(s) and s[i + 1] == "'":
                    i += 1
                else:
                    quote = None
            elif quote == '"':
                if ch == "\\":
                    i += 1
                elif ch == '"':
                    quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        i += 1
    return depth


def _preprocess(text):
    """-> list of (indent, content, lineno): comments stripped, blanks dropped,
    flow collections joined across lines until brackets balance."""
    raw = text.split("\n")
    items = []
    i = 0
    while i < len(raw):
        lineno = i + 1
        line = raw[i]
        if "\t" in line[: len(line) - len(line.lstrip())]:
            _fail(lineno, "tab in indentation (etlspec subset requires spaces)")
        stripped = _strip_comment(line, lineno)
        if not stripped.strip():
            i += 1
            continue
        depth = _bracket_delta(stripped, lineno)
        while depth > 0:
            i += 1
            if i >= len(raw):
                _fail(lineno, "unterminated flow collection ({ or [ never closed)")
            cont = _strip_comment(raw[i], i + 1)
            stripped = stripped + " " + cont.strip()
            depth = _bracket_delta(stripped, lineno)
        if depth < 0:
            _fail(lineno, "unbalanced closing bracket")
        indent = len(stripped) - len(stripped.lstrip(" "))
        content = stripped.strip()
        for marker, name in (("&", "anchor"), ("*", "alias"), ("!", "tag")):
            # only reject as syntax when a bare token starts with the marker
            if re.search(rf"(?:^|[:\s-]\s*){re.escape(marker)}\w", content) and not _in_quotes_only(content, marker):
                _fail(lineno, f"YAML {name} is outside the etlspec subset")
        if re.search(r":\s*[|>]\s*$", content):
            _fail(lineno, "block scalar (| or >) is outside the etlspec subset")
        items.append((indent, content, lineno))
        i += 1
    return items


def _in_quotes_only(content, marker):
    """True if every occurrence of marker sits inside a quoted string."""
    quote = None
    i = 0
    while i < len(content):
        ch = content[i]
        if quote:
            if quote == "'" and ch == "'":
                if i + 1 < len(content) and content[i + 1] == "'":
                    i += 1
                else:
                    quote = None
            elif quote == '"':
                if ch == "\\":
                    i += 1
                elif ch == '"':
                    quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == marker:
            return False
        i += 1
    return True


def _scalar(token, lineno):
    token = token.strip()
    if token.startswith('"'):
        if not (len(token) >= 2 and token.endswith('"')):
            _fail(lineno, f"bad double-quoted string: {token!r}")
        body = token[1:-1]
        out = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == "\\":
                i += 1
                if i >= len(body):
                    _fail(lineno, "dangling backslash in string")
                esc = body[i]
                mapped = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "/": "/"}.get(esc)
                if mapped is None:
                    _fail(lineno, f"unsupported escape \\{esc} (etlspec subset)")
                out.append(mapped)
            else:
                out.append(ch)
            i += 1
        return "".join(out)
    if token.startswith("'"):
        if not (len(token) >= 2 and token.endswith("'")):
            _fail(lineno, f"bad single-quoted string: {token!r}")
        return token[1:-1].replace("''", "'")
    if token in ("null", "~"):
        return None
    if token == "true":
        return True
    if token == "false":
        return False
    if _INT_RE.match(token):
        return int(token)
    if _FLOAT_RE.match(token):
        return float(token)
    return token


# ---- flow-collection tokenizer/parser -------------------------------

def _parse_flow(s, lineno):
    val, rest = _flow_value(s.strip(), lineno)
    if rest.strip():
        _fail(lineno, f"trailing content after flow collection: {rest.strip()!r}")
    return val


def _flow_value(s, lineno):
    s = s.lstrip()
    if s.startswith("{"):
        return _flow_map(s[1:], lineno)
    if s.startswith("["):
        return _flow_seq(s[1:], lineno)
    # bare or quoted scalar up to a stop character
    if s.startswith(('"', "'")):
        end = _quoted_end(s, lineno)
        return _scalar(s[:end], lineno), s[end:]
    j = 0
    while j < len(s) and s[j] not in _FLOW_STOP:
        j += 1
    if j < len(s) and s[j] == ":":
        _fail(lineno, f"unquoted ':' inside flow scalar near {s[:20]!r} — quote the value")
    return _scalar(s[:j], lineno), s[j:]


def _quoted_end(s, lineno):
    quote = s[0]
    i = 1
    while i < len(s):
        ch = s[i]
        if quote == "'" and ch == "'":
            if i + 1 < len(s) and s[i + 1] == "'":
                i += 2
                continue
            return i + 1
        if quote == '"':
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                return i + 1
        i += 1
    _fail(lineno, "unterminated quoted string in flow collection")


def _flow_key(s, lineno):
    s = s.lstrip()
    if s.startswith(('"', "'")):
        end = _quoted_end(s, lineno)
        key, rest = _scalar(s[:end], lineno), s[end:].lstrip()
    else:
        j = 0
        while j < len(s) and s[j] not in _FLOW_STOP:
            j += 1
        key, rest = s[:j].strip(), s[j:]
    if not rest.startswith(":"):
        _fail(lineno, f"expected ':' after flow-map key near {s[:20]!r}")
    return key, rest[1:]


def _flow_map(s, lineno):
    out = {}
    s = s.lstrip()
    if s.startswith("}"):
        return out, s[1:]
    while True:
        key, s = _flow_key(s, lineno)
        val, s = _flow_value(s, lineno)
        if key in out:
            _fail(lineno, f"duplicate key {key!r} in flow map")
        out[key] = val
        s = s.lstrip()
        if s.startswith(","):
            s = s[1:].lstrip()
            continue
        if s.startswith("}"):
            return out, s[1:]
        _fail(lineno, f"expected ',' or '}}' in flow map near {s[:20]!r}")


def _flow_seq(s, lineno):
    out = []
    s = s.lstrip()
    if s.startswith("]"):
        return out, s[1:]
    while True:
        val, s = _flow_value(s, lineno)
        out.append(val)
        s = s.lstrip()
        if s.startswith(","):
            s = s[1:].lstrip()
            continue
        if s.startswith("]"):
            return out, s[1:]
        _fail(lineno, f"expected ',' or ']' in flow sequence near {s[:20]!r}")


def _value_of(rest, lineno):
    rest = rest.strip()
    if rest.startswith(("{", "[")):
        return _parse_flow(rest, lineno)
    return _scalar(rest, lineno)


# ---- block structure ------------------------------------------------

def _parse_block(items, i, indent):
    """Parse a block map or sequence at exactly `indent`. Returns (value, next_i)."""
    if items[i][1].startswith("- ") or items[i][1] == "-":
        return _parse_seq(items, i, indent)
    return _parse_map(items, i, indent)


def _parse_map(items, i, indent):
    out = {}
    while i < len(items):
        ind, content, lineno = items[i]
        if ind < indent:
            break
        if ind > indent:
            _fail(lineno, f"unexpected indent {ind} (expected {indent})")
        if content.startswith("- ") or content == "-":
            _fail(lineno, "sequence item where a mapping key was expected")
        m = re.match(r"^(\"[^\"]*\"|'[^']*'|[^:\s][^:]*):(.*)$", content)
        if not m:
            _fail(lineno, f"expected 'key: value' or 'key:', got {content!r}")
        key = _scalar(m.group(1), lineno)
        if key in out:
            _fail(lineno, f"duplicate key {key!r}")
        rest = m.group(2)
        if rest.strip():
            out[key] = _value_of(rest, lineno)
            i += 1
        else:
            # nested block (or empty)
            if i + 1 < len(items) and items[i + 1][0] > ind:
                out[key], i = _parse_block(items, i + 1, items[i + 1][0])
            else:
                out[key] = None
                i += 1
    return out, i


def _parse_seq(items, i, indent):
    out = []
    while i < len(items):
        ind, content, lineno = items[i]
        if ind < indent:
            break
        if ind > indent:
            _fail(lineno, f"unexpected indent {ind} in sequence (expected {indent})")
        if not (content.startswith("- ") or content == "-"):
            break
        body = content[2:] if content.startswith("- ") else ""
        if not body.strip():
            _fail(lineno, "empty sequence item is outside the etlspec subset")
        if body.lstrip().startswith(("{", "[")):
            out.append(_value_of(body, lineno))
            i += 1
        elif re.match(r"^(\"[^\"]*\"|'[^']*'|[^:\s][^:{\[]*):(\s|$)", body):
            # "- key: ..." starts a block map whose keys sit at indent+2;
            # rewrite this item as the first key line and parse the map.
            items[i] = (indent + 2, body, lineno)
            val, i = _parse_map(items, i, indent + 2)
            out.append(val)
        else:
            out.append(_value_of(body, lineno))
            i += 1
    return out, i


def load_etlspec(text: str) -> dict:
    """Parse etlspec-subset YAML text into plain Python data. Fail-loud."""
    items = _preprocess(text)
    if not items:
        raise SpecError("empty spec")
    if items[0][0] != 0:
        _fail(items[0][2], "top level must start at column 0")
    val, i = _parse_block(items, 0, 0)
    if i != len(items):
        _fail(items[i][2], f"unparsed content: {items[i][1]!r}")
    if not isinstance(val, dict):
        raise SpecError("top level of an etlspec must be a mapping")
    return val


# =====================================================================
# Validation — the spec contract, enforced before any code is emitted
# =====================================================================

REQUIRED_POLICIES = ["unicode_normalization", "strip_control_chars",
                     "normalize_unicode_whitespace", "trim_whitespace",
                     "empty_string_is_null", "null_propagation",
                     "datetime_rendering", "error_disposition", "error_budget",
                     "duplicate_rows"]
PROVENANCES = {"explicit", "detected-confirmed", "default", "unconfirmed"}
COLUMN_TYPES = {"string", "integer", "decimal", "date", "datetime", "boolean"}
# op -> allowed kwargs, in canonical emission order
OPS = {
    "to_int": ["thousands_sep", "currency", "accounting_negative"],
    "to_decimal": ["thousands_sep", "currency", "accounting_negative",
                   "percent", "magnitude", "scale"],
    "to_date": ["formats"],
    "to_datetime": ["formats", "assume_tz", "to_utc"],
    "to_bool": ["mapping"],
    "format_datetime": ["fmt"],
    "constant": ["value"],
    "expr": ["python"],
}
UNSUPPORTED_OPS = {"concat", "split"}  # spec vocabulary; compiler support pending


def _require(cond, msg):
    if not cond:
        raise SpecError(msg)


def validate_spec(spec: dict):
    for key in ("etlspec", "name", "taxonomy_version", "source", "target",
                "policies", "mappings"):
        _require(key in spec, f"spec is missing required top-level key {key!r}")
    _require(re.match(r"^[a-z][a-z0-9_]*$", str(spec["name"])),
             f"spec name {spec['name']!r} must be snake_case")

    src = spec["source"]
    _require(src.get("format") == "csv",
             f"source.format {src.get('format')!r}: only 'csv' is supported")
    _require(src.get("header", "present") == "present",
             "source.header 'absent' is not supported by runtime v0.2 "
             "(it always reads a header row) — declare header: present")
    _require(isinstance(src.get("expected_columns"), list) and src["expected_columns"],
             "source.expected_columns must be a non-empty list")

    pol = spec["policies"]
    for key in REQUIRED_POLICIES:
        _require(key in pol, f"policies.{key} is missing — every spec records every "
                             "policy, including defaults (spec-format rule 1)")
        entry = pol[key]
        _require(isinstance(entry, dict) and "value" in entry and "provenance" in entry,
                 f"policies.{key} must be {{value, provenance}}")
        _require(entry["provenance"] in PROVENANCES,
                 f"policies.{key}.provenance {entry['provenance']!r} not in {sorted(PROVENANCES)}")

    cols = spec["target"].get("columns")
    _require(isinstance(cols, list) and cols, "target.columns must be a non-empty list")
    col_by_name = {}
    for c in cols:
        _require(isinstance(c, dict) and "name" in c and "type" in c and "nullable" in c,
                 f"target column {c!r} needs name/type/nullable")
        _require(c["type"] in COLUMN_TYPES,
                 f"target column {c['name']!r}: unknown type {c['type']!r}")
        _require(c["name"] not in col_by_name, f"duplicate target column {c['name']!r}")
        col_by_name[c["name"]] = c

    expected = set(src["expected_columns"])
    mapped = set()
    for m in spec["mappings"]:
        _require("target" in m, f"mapping {m!r} missing target")
        t = m["target"]
        _require(t in col_by_name, f"mapping targets unknown column {t!r}")
        _require(t not in mapped, f"target column {t!r} mapped twice")
        mapped.add(t)
        transforms = m.get("transforms") or []
        is_constant = len(transforms) == 1 and transforms[0].get("op") == "constant"
        if not is_constant:
            _require("source" in m, f"mapping for {t!r} missing source")
            _require(m["source"] in expected,
                     f"mapping for {t!r}: source {m['source']!r} not in expected_columns")
        for tr in transforms:
            op = tr.get("op")
            _require(op not in UNSUPPORTED_OPS,
                     f"op {op!r} is not yet supported by the compiler — use "
                     "{op: expr, python: ...} (loud escape hatch) instead")
            _require(op in OPS, f"mapping for {t!r}: unknown transform op {op!r}")
            for kwarg in tr:
                _require(kwarg == "op" or kwarg in OPS[op],
                         f"op {op!r}: unknown argument {kwarg!r}")
        for d in m.get("decisions") or []:
            _require("id" in d and "provenance" in d,
                     f"decision {d!r} on {t!r} needs id + provenance")

    declared_unfilled = set((spec.get("unmapped") or {}).get("unfilled_target_columns") or [])
    for name in col_by_name:
        _require(name in mapped or name in declared_unfilled,
                 f"target column {name!r} has no mapping and is not declared in "
                 "unmapped.unfilled_target_columns — silence is a bug")
    return col_by_name


# =====================================================================
# Emission — deterministic: same spec bytes -> byte-identical pipeline
# =====================================================================

_MAX_LINE = 96


def _kwargs_text(tr, op, extra=()):
    parts = []
    for k in OPS[op]:
        if k in tr:
            parts.append(f"{k}={tr[k]!r}")
    parts.extend(extra)
    return ", ".join(parts)


def _mapping_comment(m):
    bits = []
    for d in m.get("decisions") or []:
        choice = d.get("choice")
        seg = f"{d['id']}: {choice}" if choice is not None else d["id"]
        bits.append(f"{seg} ({d['provenance']})")
    sent = m.get("sentinels")
    if sent:
        bits.append(f"sentinels {sent['values']!r} ({sent['provenance']})")
    src = m.get("source", "(constant)")
    tail = f"  [{'; '.join(bits)}]" if bits else ""
    return f"    # {m['target']} <- {src}{tail}"


def _emit_mapping(m, col, helpers):
    """Returns list of code lines for one mapping."""
    target, src = m["target"], m.get("source")
    transforms = m.get("transforms") or []
    expr = f"row[{src!r}]" if src is not None else None
    for tr in transforms:
        op = tr["op"]
        if op == "constant":
            expr = repr(tr["value"])
        elif op == "expr":
            fn = f"_expr_{target}"
            helpers.append(
                f"def {fn}(row):\n"
                f"    # CUSTOM EXPRESSION from the spec (op: expr) — the only\n"
                f"    # non-runtime logic permitted in a generated pipeline.\n"
                f"    return {tr['python']}\n")
            expr = f"{fn}(row)"
        elif op == "to_decimal":
            tr = dict(tr)
            if "scale" not in tr and col.get("type") == "decimal" and "scale" in col:
                tr["scale"] = col["scale"]  # TYP-08: target scale drives quantization
            expr = f"rt.to_decimal({expr}, {src!r}, {_kwargs_text(tr, op)})"
        elif op == "to_bool":
            expr = (f"rt.to_bool({expr}, {src!r}, "
                    f"{_kwargs_text(tr, op, extra=['report=report'])})")
        else:
            expr = f"rt.{op}({expr}, {src!r}, {_kwargs_text(tr, op)})"
    if col.get("max_length") is not None:
        expr = f"rt.check_length({expr}, {src!r}, max_length={col['max_length']!r})"
    if col.get("nullable") is False:
        expr = f"rt.not_null({expr}, {src!r})"  # NUL-04

    lines = [_mapping_comment(m)]
    one = f"    out[{target!r}] = {expr}"
    if len(one) <= _MAX_LINE:
        lines.append(one)
    else:
        # deterministic stepwise form for long chains
        lines.append(f"    v = row[{src!r}]" if src is not None else "    v = None")
        lines.extend(f"    {s}" for s in _stepwise(m, col))
        lines.append(f"    out[{target!r}] = v")
    return lines


def _stepwise(m, col):
    src = m.get("source")
    steps = []
    for tr in m.get("transforms") or []:
        op = tr["op"]
        if op == "constant":
            steps.append(f"v = {tr['value']!r}")
        elif op == "expr":
            steps.append(f"v = _expr_{m['target']}(row)")
        elif op == "to_decimal":
            tr = dict(tr)
            if "scale" not in tr and col.get("type") == "decimal" and "scale" in col:
                tr["scale"] = col["scale"]
            steps.append(f"v = rt.to_decimal(v, {src!r}, {_kwargs_text(tr, op)})")
        elif op == "to_bool":
            steps.append(f"v = rt.to_bool(v, {src!r}, "
                         f"{_kwargs_text(tr, op, extra=['report=report'])})")
        else:
            steps.append(f"v = rt.{op}(v, {src!r}, {_kwargs_text(tr, op)})")
    if col.get("max_length") is not None:
        steps.append(f"v = rt.check_length(v, {src!r}, max_length={col['max_length']!r})")
    if col.get("nullable") is False:
        steps.append(f"v = rt.not_null(v, {src!r})")
    return steps


def compile_spec(spec: dict, *, spec_bytes: bytes, spec_filename: str) -> str:
    """spec (parsed) -> pipeline.py source text. Deterministic; no wall clock."""
    col_by_name = validate_spec(spec)
    sha = hashlib.sha256(spec_bytes).hexdigest()
    name = spec["name"]
    src = spec["source"]
    dialect = src.get("dialect") or {}
    pol = spec["policies"]

    policies = {k: pol[k]["value"] for k in REQUIRED_POLICIES}
    sentinels = {}
    for m in spec["mappings"]:
        sent = m.get("sentinels")
        if sent:
            sentinels[m["source"]] = list(sent["values"])
    if sentinels:
        policies["sentinels"] = sentinels

    config = {
        "name": name,
        "spec_version": str(spec["etlspec"]),
        "generator_version": f"etl-spec-compiler/{COMPILER_VERSION}",
        "encoding": (src.get("encoding") or {}).get("value", "utf-8"),
        "delimiter": dialect.get("delimiter", ","),
        "quotechar": dialect.get("quotechar", '"'),
        "expected_columns": list(src["expected_columns"]),
        "output_columns": [c["name"] for c in spec["target"]["columns"]],
        "policies": policies,
    }

    review = spec.get("review_required") or []
    review_block = ""
    if review:
        review_block = ("\nREVIEW REQUIRED — unconfirmed decisions (unattended mode):\n"
                        + "".join(f"  - {r!r}\n" for r in review))

    mapping_by_target = {m["target"]: m for m in spec["mappings"]}
    helpers: list = []
    body_lines = []
    for c in spec["target"]["columns"]:
        m = mapping_by_target.get(c["name"])
        if m is None:
            body_lines.append(f"    # {c['name']} — unfilled (declared in spec.unmapped)")
            body_lines.append(f"    out[{c['name']!r}] = None")
        else:
            body_lines.extend(_emit_mapping(m, c, helpers))

    config_lines = ["CONFIG = {"]
    for k, v in config.items():
        if k == "policies":
            config_lines.append("    \"policies\": {")
            for pk, pv in v.items():
                config_lines.append(f"        {pk!r}: {pv!r},")
            config_lines.append("    },")
        else:
            config_lines.append(f"    {k!r}: {v!r},")
    config_lines.append("}")

    parts = [
        "#!/usr/bin/env python3",
        f'"""{name} pipeline — GENERATED by etl-spec-compiler {COMPILER_VERSION}. DO NOT EDIT.',
        "",
        f"Source spec: {spec_filename}",
        f"Spec sha256: {sha}",
        f"Spec format: {spec['etlspec']} · authored against taxonomy v{spec['taxonomy_version']}",
        "",
        "Regenerate (byte-identical for identical spec bytes):",
        f"    python3 compile_spec.py {spec_filename}",
        "Edit the spec, never this file — hand-edits and the spec drift apart.",
        "Error codes in reports are ETL Failure-Mode Taxonomy IDs."
        + review_block,
        '"""',
        "import argparse",
        "",
        "import etl_runtime as rt",
        "",
        "# ---- Resolved configuration (from the spec — edit the spec, not this) ----",
        *config_lines,
        "",
        "",
        *(helpers + [""] if helpers else []),
        "def transform_row(row, report):",
        '    """Map one cleaned input row -> output row. Values arrive text-cleaned',
        "    and null-resolved per CONFIG policies (ENC-03/04/05, NUL-01/02/03),",
        '    applied and counted by the runtime."""',
        "    out = {}",
        *body_lines,
        "    return out",
        "",
        "",
        "def main():",
        "    p = argparse.ArgumentParser(description=__doc__)",
        '    p.add_argument("input")',
        '    p.add_argument("--out-dir", default="./etl_out")',
        "    args = p.parse_args()",
        "    result = rt.run_pipeline(input_path=args.input, out_dir=args.out_dir,",
        "                             config=CONFIG, transform_row=transform_row)",
        "    raise SystemExit(result.exit_code)",
        "",
        "",
        'if __name__ == "__main__":',
        "    main()",
        "",
    ]
    return "\n".join(parts)


# =====================================================================
# CLI
# =====================================================================

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", help="output pipeline path "
                    "(default: <name>_pipeline.py beside the spec)")
    args = ap.parse_args(argv)
    with open(args.spec, encoding="utf-8") as f:
        text = f.read()
    spec = load_etlspec(text)
    code = compile_spec(spec, spec_bytes=text.encode("utf-8"),
                        spec_filename=os.path.basename(args.spec))
    out = args.out or os.path.join(os.path.dirname(args.spec) or ".",
                                   f"{spec['name']}_pipeline.py")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(code)
    print(f"wrote {out} (compiler {COMPILER_VERSION}, "
          f"spec sha256 {hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
