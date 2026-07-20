#!/usr/bin/env python3
"""profile.py — detect ETL failure modes in a tabular sample file.

Emits findings keyed by taxonomy IDs (see references/taxonomy.md). Each finding
carries evidence and a class:
  fix        -> safe auto-repair; the interview only summarizes these
  ask        -> meaning-changing; the interview MUST raise these with the user
  row-error  -> handled at runtime per error disposition
  info       -> contextual (dialect, encoding) recorded into the spec

Usage: python profile.py SAMPLE.csv [--json OUT.json] [--max-rows 1000]
Stdlib only.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import unicodedata
from collections import Counter

MAX_ROWS_DEFAULT = 1000

KNOWN_SENTINELS = {"n/a", "na", "null", "none", "nil", "-", "--", ".", "?", "#n/a",
                   "unknown", "(blank)", "missing"}
NUMERIC_SENTINEL_CANDIDATES = {"9999", "99999", "999999", "-1", "0000", "1900-01-01",
                               "1970-01-01", "00000000"}
BOOL_VOCABS = [
    {"y", "n"}, {"yes", "no"}, {"true", "false"}, {"t", "f"}, {"0", "1"}, {"x", ""},
]
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
UNICODE_WS_RE = re.compile("[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000\u200b\u200c\u200d\u2060]")
MOJIBAKE_RE = re.compile("(Ã[©¨«¢£±³‰€¤¶ºµ]|â€[™œ˜¦“”™]|Â[°£®©±])")
THOUSANDS_RE = re.compile(r"^-?[$€£¥₹]?\s?\d{1,3}(,\d{3})+(\.\d+)?$")
CURRENCY_RE = re.compile(r"^\s*[$€£¥₹]")
PAREN_NEG_RE = re.compile(r"^\(\s*[$€£¥₹]?\s*[\d,._]+\s*\)$")
PERCENT_RE = re.compile(r"^-?\d+(\.\d+)?%$")
LEADING_ZERO_RE = re.compile(r"^0\d+$")
PLAIN_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
DATE_SLASH_RE = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")
FOOTER_KEYWORDS = re.compile(r"(?i)^(sub)?total|^sum\b|^count\b|^generated|^report|^page \d")


def finding(fid, klass, message, column=None, count=None, evidence=None):
    f = {"id": fid, "class": klass, "message": message}
    if column is not None:
        f["column"] = column
    if count is not None:
        f["count"] = count
    if evidence:
        f["evidence"] = evidence
    return f


# ---------------------------------------------------------------------- bytes/encoding

def profile_bytes(raw: bytes, findings: list) -> str:
    encoding = "utf-8"
    if raw.startswith(b"\xef\xbb\xbf"):
        findings.append(finding("ENC-02", "fix", "UTF-8 BOM present; default: strip"))
        raw = raw[3:]
    elif raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        findings.append(finding("ENC-01", "ask", "UTF-16 BOM detected — confirm encoding",
                                evidence="leading bytes " + raw[:2].hex()))
        return "utf-16"
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as e:
        try:
            raw.decode("latin-1")
            encoding = "latin-1"
            findings.append(finding(
                "ENC-01", "ask",
                "not valid UTF-8; decodable as latin-1/cp1252 — confirm source encoding",
                evidence=f"first decode error at byte {e.start}: {e.reason}"))
        except UnicodeDecodeError:
            findings.append(finding("ENC-01", "ask", "encoding could not be determined"))
    return encoding


def profile_text(text: str, findings: list) -> str:
    terms = {"\r\n": text.count("\r\n")}
    terms["\r"] = text.count("\r") - terms["\r\n"]
    terms["\n"] = text.count("\n") - terms["\r\n"]
    present = [t for t, n in terms.items() if n > 0]
    if len(present) > 1:
        findings.append(finding("STR-07", "fix", "mixed line endings; default: normalize",
                                evidence={k.encode('unicode_escape').decode(): v
                                          for k, v in terms.items() if v}))
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ---------------------------------------------------------------------- structure

def sniff_dialect(text: str, findings: list):
    sample = text[:8192]
    delim = ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delim = dialect.delimiter
        if delim != ",":
            findings.append(finding("STR-01", "ask",
                                    f"delimiter appears to be {delim!r} — confirm",
                                    evidence=f"sniffed from first {len(sample)} chars"))
    except csv.Error:
        findings.append(finding("STR-01", "ask", "could not sniff dialect — confirm delimiter"))
    return delim


def profile_structure(rows: list, findings: list):
    if not rows:
        return [], []
    header = rows[0]
    stripped = [h.strip() for h in header]
    if stripped != header:
        findings.append(finding("STR-04", "fix",
                                "header names carry leading/trailing whitespace; default: trim",
                                evidence=[h for h in header if h.strip() != h]))
    dupes = [h for h, n in Counter(stripped).items() if n > 1]
    if dupes:
        findings.append(finding("STR-04", "ask", f"duplicate column name(s): {dupes}"))
    blanks = sum(1 for h in stripped if not h)
    if blanks:
        findings.append(finding("STR-04", "ask", f"{blanks} blank header cell(s)"))

    data = rows[1:]
    width = len(header)
    ragged = [(i + 2, len(r)) for i, r in enumerate(data)
              if any(f.strip() for f in r) and len(r) != width]
    if ragged:
        findings.append(finding("STR-02", "row-error",
                                f"{len(ragged)} ragged row(s); default: quarantine",
                                count=len(ragged), evidence=ragged[:5]))
    blank_rows = [i + 2 for i, r in enumerate(data) if not any(f.strip() for f in r)]
    if blank_rows:
        findings.append(finding("STR-06", "fix",
                                f"{len(blank_rows)} fully blank row(s); default: skip and count",
                                count=len(blank_rows)))
    # footer heuristic: last non-blank row starting with an aggregate keyword
    for i in range(len(data) - 1, -1, -1):
        r = data[i]
        if any(f.strip() for f in r):
            if r and FOOTER_KEYWORDS.search(r[0].strip()):
                findings.append(finding("STR-06", "ask",
                                        f"row {i + 2} looks like a footer/total row — confirm exclusion",
                                        evidence=r[:4]))
            break
    exact_dupes = sum(n - 1 for n in Counter(map(tuple, data)).values() if n > 1)
    if exact_dupes:
        findings.append(finding("STR-05", "ask",
                                f"{exact_dupes} exact duplicate row(s); default: keep and report",
                                count=exact_dupes))
    cols = {}
    for ci, name in enumerate(stripped):
        cols[name or f"__col{ci}__"] = [r[ci] if ci < len(r) else None
                                        for r in data
                                        if any(f.strip() for f in r) and len(r) == width]
    return stripped, cols


# ---------------------------------------------------------------------- per-column

def profile_characters(name: str, values: list, findings: list):
    counts = Counter()
    moji_examples = []
    for v in values:
        if not v:
            continue
        if CONTROL_RE.search(v):
            counts["ENC-04"] += 1
        if UNICODE_WS_RE.search(v):
            counts["ENC-05"] += 1
        if unicodedata.normalize("NFC", v) != v:
            counts["ENC-03"] += 1
        if MOJIBAKE_RE.search(v):
            counts["ENC-06"] += 1
            if len(moji_examples) < 3:
                moji_examples.append(v[:60])
    for fid, n in counts.items():
        if fid == "ENC-06":
            findings.append(finding(fid, "ask",
                                    "likely mojibake (prior mis-decode); repair is opt-in",
                                    column=name, count=n, evidence=moji_examples))
        else:
            findings.append(finding(fid, "fix", "character cleanup applies", column=name, count=n))


def profile_nulls(name: str, values: list, findings: list):
    empties = sum(1 for v in values if v == "")
    ws_only = sum(1 for v in values if v is not None and v != "" and v.strip() == "")
    if empties:
        findings.append(finding("NUL-01", "ask" , f"{empties} empty value(s) — null or empty string?",
                                column=name, count=empties))
    if ws_only:
        findings.append(finding("NUL-02", "fix", f"{ws_only} whitespace-only value(s); default: trim",
                                column=name, count=ws_only))
    trimmed = [v.strip() for v in values if v is not None and v.strip()]
    hits = Counter(v for v in trimmed if v.casefold() in KNOWN_SENTINELS)
    total = len(trimmed)
    if total:
        freq = Counter(trimmed)
        for cand in NUMERIC_SENTINEL_CANDIDATES:
            n = freq.get(cand, 0)
            if n and n / total > 0.01 and n >= 3:
                hits[cand] = n
    if hits:
        findings.append(finding("NUL-03", "ask",
                                "possible sentinel value(s) — confirm which mean null",
                                column=name, evidence=dict(hits.most_common(8))))


def profile_types(name: str, values: list, findings: list):
    vals = [v.strip() for v in values if v is not None and v.strip()]
    vals = [v for v in vals if v.casefold() not in KNOWN_SENTINELS]
    if not vals:
        return
    n = len(vals)

    # TYP-06 boolean vocabulary
    lowered = {v.casefold() for v in vals}
    for vocab in BOOL_VOCABS:
        if lowered <= vocab and len(lowered) >= 1 and vocab != {"0", "1"} or lowered == {"0", "1"}:
            if lowered <= vocab:
                findings.append(finding("TYP-06", "ask",
                                        f"boolean-like vocabulary {sorted(lowered)} — confirm truth mapping",
                                        column=name))
                return

    # TYP-01 formatted numerics
    fmt_counts = Counter()
    for v in vals:
        if THOUSANDS_RE.match(v):
            fmt_counts["thousands-separators"] += 1
        if CURRENCY_RE.match(v) and any(c.isdigit() for c in v):
            fmt_counts["currency-symbol"] += 1
        if PAREN_NEG_RE.match(v):
            fmt_counts["accounting-negative"] += 1
        if PERCENT_RE.match(v):
            fmt_counts["percent-suffix"] += 1
    if fmt_counts:
        findings.append(finding("TYP-01", "ask",
                                "formatted numeric pattern(s) — confirm cleaning rules",
                                column=name, evidence=dict(fmt_counts)))

    # TYP-07 numeric-looking identifiers
    digit_vals = [v for v in vals if v.isdigit()]
    if digit_vals and len(digit_vals) / n > 0.9:
        leading_zeros = sum(1 for v in digit_vals if LEADING_ZERO_RE.match(v))
        widths = {len(v) for v in digit_vals}
        if leading_zeros:
            findings.append(finding("TYP-07", "ask",
                                    "leading zeros present — keep as string to avoid data loss",
                                    column=name, count=leading_zeros,
                                    evidence=[v for v in digit_vals if LEADING_ZERO_RE.match(v)][:3]))
        elif len(widths) == 1 and widths.pop() >= 6 and len(set(digit_vals)) / len(digit_vals) > 0.9:
            findings.append(finding("TYP-07", "ask",
                                    "uniform-width, high-cardinality digits — identifier? keep as string",
                                    column=name))

    # TYP-03 date format ambiguity
    slashy = [v for v in vals if DATE_SLASH_RE.match(v)]
    if slashy and len(slashy) / n > 0.6:
        sep = "/" if "/" in slashy[0] else "-"
        mdy = dmy = both = bad = 0
        for v in slashy:
            a, b, _ = v.split(sep)
            a, b = int(a), int(b)
            m_ok, d_ok = a <= 12, b <= 12
            if m_ok and d_ok:
                both += 1
            elif m_ok:
                mdy += 1
            elif d_ok:
                dmy += 1
            else:
                bad += 1
        if mdy and dmy:
            findings.append(finding("TYP-03", "ask",
                                    "date column contains BOTH MDY-only and DMY-only values — mixed formats?",
                                    column=name, evidence={"mdy_only": mdy, "dmy_only": dmy}))
        elif mdy:
            findings.append(finding("TYP-03", "ask",
                                    f"dates consistent with MDY ({mdy} decisive, {both} ambiguous) — confirm",
                                    column=name, evidence={"decisive_mdy": mdy, "ambiguous": both}))
        elif dmy:
            findings.append(finding("TYP-03", "ask",
                                    f"dates consistent with DMY ({dmy} decisive, {both} ambiguous) — confirm",
                                    column=name, evidence={"decisive_dmy": dmy, "ambiguous": both}))
        else:
            findings.append(finding("TYP-03", "ask",
                                    f"date format fully ambiguous ({both} values parse as both MDY and DMY) — must confirm",
                                    column=name, evidence={"ambiguous": both}))

    # TYP-02 decimal locale
    eu_style = sum(1 for v in vals if re.match(r"^-?\d{1,3}(\.\d{3})+(,\d+)?$", v))
    if eu_style:
        findings.append(finding("TYP-02", "ask",
                                "possible EU-style decimal/grouping (1.234,56) — confirm locale",
                                column=name, count=eu_style))

    # TYP-10 case/trim collisions in low-cardinality columns
    distinct = set(vals)
    if 1 < len(distinct) <= 20 and not all(PLAIN_NUMBER_RE.match(v) for v in distinct):
        groups = {}
        for v in distinct:
            groups.setdefault(v.strip().casefold(), set()).add(v)
        collisions = {k: sorted(g) for k, g in groups.items() if len(g) > 1}
        if collisions:
            findings.append(finding("TYP-10", "ask",
                                    "values collide under trim+casefold — canonicalize?",
                                    column=name, evidence=collisions))


# ---------------------------------------------------------------------- main

def profile_file(path: str, max_rows: int = MAX_ROWS_DEFAULT) -> dict:
    findings: list = []
    raw = open(path, "rb").read()
    encoding = profile_bytes(raw, findings)
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode(encoding, errors="replace")
    text = profile_text(text, findings)
    delim = sniff_dialect(text, findings)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = []
    for i, r in enumerate(reader):
        if i > max_rows:
            break
        rows.append(r)
    header, cols = profile_structure(rows, findings)
    for name, values in cols.items():
        profile_characters(name, values, findings)
        profile_nulls(name, values, findings)
        profile_types(name, values, findings)

    order = {"ask": 0, "row-error": 1, "fix": 2, "info": 3}
    findings.sort(key=lambda f: (order.get(f["class"], 9), f["id"], f.get("column") or ""))
    return {
        "file": path,
        "encoding": encoding,
        "delimiter": delim,
        "columns": header,
        "rows_profiled": max(0, len(rows) - 1),
        "findings": findings,
        "summary": {
            "ask": sum(1 for f in findings if f["class"] == "ask"),
            "fix": sum(1 for f in findings if f["class"] == "fix"),
            "row-error": sum(1 for f in findings if f["class"] == "row-error"),
        },
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    ap.add_argument("--json", dest="json_out", help="write findings JSON here")
    ap.add_argument("--max-rows", type=int, default=MAX_ROWS_DEFAULT)
    args = ap.parse_args()
    result = profile_file(args.path, args.max_rows)
    out = json.dumps(result, indent=2, ensure_ascii=False)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            f.write(out)
    print(out)


if __name__ == "__main__":
    sys.exit(main())
