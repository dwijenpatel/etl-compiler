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
from typing import Sequence, TypedDict

# ---------------------------------------------------------------------------
# Typed vocabulary. Functional TypedDict syntax is required: "class" is a
# Python keyword and "row-error" contains a hyphen — both are part of the
# findings contract consumed by SKILL.md and the eval harnesses, so the JSON
# shape wins over class-syntax convenience.
# ---------------------------------------------------------------------------

Finding = TypedDict("Finding", {
    "id": str,            # taxonomy ID (e.g. "TYP-03")
    "class": str,         # fix | ask | row-error | info
    "message": str,
    "column": str,
    "count": int,
    "evidence": object,   # heterogeneous per detector: examples, counts, pairs
    "group_key": str,     # findings sharing (id, group_key) = one interview question
}, total=False)

InterviewGroup = TypedDict("InterviewGroup", {
    "id": str,
    "class": str,
    "group_key": str | None,
    "message": str,
    "columns": list[str],
    "evidence": dict[str, object],
    "n_columns": int,
}, total=False)

ProfileSummary = TypedDict("ProfileSummary", {
    "ask": int,           # raw ask findings (one per column)
    "ask_questions": int, # grouped interview questions actually posed
    "fix": int,
    "row-error": int,
})

CharsetCandidate = TypedDict("CharsetCandidate", {
    "encoding": str,
    "score": float,       # decoded-text plausibility in [0, 1]; see _score_decoding
})

ProfileResult = TypedDict("ProfileResult", {
    "file": str,
    "encoding": str,
    "delimiter": str,
    "columns": list[str],
    "rows_profiled": int,
    "findings": list[Finding],
    "interview_groups": list[InterviewGroup],
    "summary": ProfileSummary,
})

MAX_ROWS_DEFAULT = 1000
SNIFF_DELIMITERS = ",;\t|"       # candidate delimiters the sniffer tries (STR-01)

# ENC-01 charset candidates for non-UTF-8 bytes, in prior order (ties break
# toward the earlier entry). cp1252 rejects 5 code points latin-1 accepts, so
# both are listed; shift_jis/euc_jp/gb18030 cover the CJK exports the corpus
# actually contains (JP portal files were previously mislabeled latin-1).
CHARSET_CANDIDATES = ("shift_jis", "euc_jp", "gb18030", "cp1252", "latin-1")
_CHARSET_SAMPLE_BYTES = 65536

KNOWN_SENTINELS = {"n/a", "n.a.", "na", "null", "none", "nil", "-", "--", "---", ".",
                   "?", "#n/a", "unknown", "(blank)", "(null)", "(none)", "(empty)",
                   "missing", "not available", "not applicable", "tbd", "xx", "\\n"}
NUMERIC_SENTINEL_CANDIDATES = {"9999", "99999", "999999", "-1", "0000", "1900-01-01",
                               "1970-01-01", "00000000"}
BOOL_VOCABS = [
    {"y", "n"}, {"yes", "no"}, {"true", "false"}, {"t", "f"}, {"0", "1"}, {"x", ""},
]
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
UNICODE_WS_RE = re.compile("[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000\u200b\u200c\u200d\u2060\ufeff]")
MOJIBAKE_RE = re.compile("(Ã[©¨«¢£±³‰€¤¶ºµ]|â€[™œ˜¦“”™]|Â[°£®©±])")
THOUSANDS_RE = re.compile(r"^-?[$€£¥₹]?\s?\d{1,3}(,\d{3})+(\.\d+)?$")
CURRENCY_RE = re.compile(r"^\s*[$€£¥₹]")
PAREN_NEG_RE = re.compile(r"^\(\s*[$€£¥₹]?\s*[\d,._]+\s*\)$")
PERCENT_RE = re.compile(r"^-?\d+(\.\d+)?%$")
# Magnitude-suffixed numerics: 10.00K, 1.2M, 3B, $5.4k (NOAA-style scaled strings). TYP-01.
MAGNITUDE_RE = re.compile(r"^\s*[$€£¥₹]?\s?-?\d+(?:[.,]\d+)?\s?[KkMmBbGgTt]$")
LEADING_ZERO_RE = re.compile(r"^0\d+$")
PLAIN_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
# Ambiguous D/M dates, separators / - or . (European dotted DD.MM.YYYY included). TYP-03.
DATE_SLASH_RE = re.compile(r"^\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}$")
DATE_SPLIT_RE = re.compile(r"[/.\-]")   # splits a matched date on its own separator(s)
FOOTER_KEYWORDS = re.compile(r"(?i)^(sub)?total|^sum\b|^count\b|^generated|^report|^page \d")


def _is_blank_row(r: Sequence[str]) -> bool:
    """A row is blank when every field is empty/whitespace — the STR-06 definition,
    shared so STR-05/STR-06 and the runtime stay aligned by construction, not comment."""
    return not any(f.strip() for f in r)


def finding(fid: str, klass: str, message: str, column: str | None = None,
            count: int | None = None, evidence: object = None,
            group_key: str | None = None) -> Finding:
    f: Finding = {"id": fid, "class": klass, "message": message}
    if column is not None:
        f["column"] = column
    if count is not None:
        f["count"] = count
    if evidence:
        f["evidence"] = evidence
    if group_key is not None:
        # Findings sharing (id, group_key) represent the SAME decision across
        # columns and collapse into one interview question (see group_findings).
        f["group_key"] = group_key
    return f


# ---------------------------------------------------------------------- bytes/encoding

def profile_bytes(raw: bytes, findings: list[Finding]) -> str:
    encoding = "utf-8"
    if raw.startswith(b"\xef\xbb\xbf"):
        # Strip every leading UTF-8 BOM: corpus found files carrying two (a BOM
        # prepended to an already-BOM'd export), which left one contaminating the
        # first header name.
        n_bom = 0
        while raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
            n_bom += 1
        findings.append(finding("ENC-02", "fix",
                                f"UTF-8 BOM present (×{n_bom}); default: strip"
                                if n_bom > 1 else "UTF-8 BOM present; default: strip"))
    elif raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        findings.append(finding("ENC-01", "ask", "UTF-16 BOM detected — confirm encoding",
                                evidence="leading bytes " + raw[:2].hex()))
        return "utf-16"
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as e:
        candidates = _rank_charsets(raw[:_CHARSET_SAMPLE_BYTES])
        if candidates:
            best = candidates[0]
            encoding = best["encoding"]
            findings.append(finding(
                "ENC-01", "ask",
                f"not valid UTF-8; best candidate encoding is {best['encoding']!r} "
                f"(score {best['score']}) — confirm source encoding",
                evidence={"decode_error": f"byte {e.start}: {e.reason}",
                          "candidates": candidates}))
        else:
            findings.append(finding("ENC-01", "ask", "encoding could not be determined"))
    return encoding


def _score_decoding(text: str) -> float:
    """Plausibility of a decoded sample, in [0, 1]. Pure. Penalizes replacement
    chars, C0 controls (beyond tab/newline/CR), and the C1 block U+0080–U+009F —
    the fingerprint of CJK bytes misread as Latin-1 (a latin-1 decode never
    fails, so byte legality alone cannot discriminate; text shape can)."""
    if not text:
        return 0.0
    bad = sum(1 for ch in text
              if ch == "�"
              or (ord(ch) < 0x20 and ch not in "\t\n\r")
              or 0x7f <= ord(ch) <= 0x9f)
    return round(1.0 - bad / len(text), 4)


def _rank_charsets(sample: bytes) -> list[CharsetCandidate]:
    """Score each candidate that strictly decodes the sample; best first.
    Ties break toward CHARSET_CANDIDATES order (sort is stable)."""
    out: list[CharsetCandidate] = []
    for enc in CHARSET_CANDIDATES:
        try:
            decoded = sample.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        out.append({"encoding": enc, "score": _score_decoding(decoded)})
    out.sort(key=lambda c: -c["score"])
    return out


def profile_text(text: str, findings: list[Finding]) -> str:
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

def sniff_dialect(text: str, findings: list[Finding]) -> str:
    sample = text[:8192]
    delim = ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=SNIFF_DELIMITERS)
        delim = dialect.delimiter
        if delim != ",":
            findings.append(finding("STR-01", "ask",
                                    f"delimiter appears to be {delim!r} — confirm",
                                    evidence=f"sniffed from first {len(sample)} chars"))
    except csv.Error:
        # No delimiter candidate found. A genuine single-column file (no delimiter
        # in any line) is not a problem — don't raise a spurious STR-01 ask (F8);
        # only ask when some line actually contains a candidate delimiter.
        head = "\n".join(sample.splitlines()[:20])
        if any(d in head for d in SNIFF_DELIMITERS):
            findings.append(finding("STR-01", "ask",
                                    "could not sniff dialect — confirm delimiter"))
    return delim


def _is_numeric_cell(v: str) -> bool:
    return bool(PLAIN_NUMBER_RE.match(v.replace(",", "").replace(" ", "")))


def detect_preamble(rows: list[list[str]]) -> int:
    """STR-06: count leading report-style metadata/title rows above the real data.
    Two signals, both conservative (require real body rows to remain):

    A. Width break — leading rows narrower than the file's modal width (a title
       cell above a wide table; the classic Excel-export shape).
    B. Type-stabilization break — same-width key-value metadata above typed data
       (the ONS time-series shape): a body column is strongly numeric, and ≥2
       contiguous leading rows violate that type. The ≥2 threshold keeps an
       ordinary single text header from being mistaken for preamble."""
    if len(rows) < 4:
        return 0
    widths = Counter(len(r) for r in rows)
    modal_width, modal_count = widths.most_common(1)[0]
    if modal_width < 2:
        return 0

    # Signal A: narrow leading rows.
    n = 0
    for r in rows:
        if len(r) < modal_width and not _is_blank_row(r):
            n += 1
        else:
            break
    if 0 < n < len(rows) - 1:
        return n

    # Signal B: type break in a body column. Scoped to 2-column key-value files —
    # the metadata-block shape (ONS time series) that signal A can't see because
    # preamble and data share a width. Wider tables with preamble present narrow
    # title rows and are handled by signal A; restricting B here avoids mistaking
    # a formatted-numeric data column (e.g. "$1,234.56", "(500)") for a type break.
    if modal_width != 2 or modal_count / len(rows) < 0.7:
        return 0
    modal_rows = [r for r in rows if len(r) == modal_width]
    body = modal_rows[len(modal_rows) // 2:]
    for ci in range(modal_width):
        body_vals = [r[ci].strip() for r in body if r[ci].strip()]
        if len(body_vals) < 3:
            continue
        if sum(_is_numeric_cell(v) for v in body_vals) / len(body_vals) > 0.9:
            # Count the lead over the FULL rows list (the caller slices
            # rows[preamble:]), so a blank separator line between the metadata
            # block and the data is counted, not skipped (F3).
            lead = 0
            for r in rows:
                v = r[ci].strip() if ci < len(r) else ""
                if v and _is_numeric_cell(v):
                    break
                lead += 1
            if 2 <= lead < len(rows) - 1:
                return lead
    return 0


def profile_structure(rows: list[list[str]], findings: list[Finding]
                      ) -> tuple[list[str], dict[str, list[str | None]]]:
    if not rows:
        return [], {}
    preamble = detect_preamble(rows)
    if preamble:
        findings.append(finding("STR-06", "ask",
                                f"{preamble} preamble/metadata row(s) above the header "
                                "(narrower than the data) — confirm the true header row",
                                count=preamble, evidence=[r[:4] for r in rows[:preamble]]))
        rows = rows[preamble:]
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
    blank_rows = [i + 2 for i, r in enumerate(data) if _is_blank_row(r)]
    if blank_rows:
        findings.append(finding("STR-06", "fix",
                                f"{len(blank_rows)} fully blank row(s); default: skip and count",
                                count=len(blank_rows)))
    # footer heuristic: last non-blank row starting with an aggregate keyword
    for i in range(len(data) - 1, -1, -1):
        r = data[i]
        if not _is_blank_row(r):
            if r and FOOTER_KEYWORDS.search(r[0].strip()):
                findings.append(finding("STR-06", "ask",
                                        f"row {i + 2} looks like a footer/total row — confirm exclusion",
                                        evidence=r[:4]))
            break
    # STR-05: exclude fully-blank rows (they are STR-06); matches the runtime,
    # which skips blanks before the duplicate check (F6).
    nonblank = [r for r in data if not _is_blank_row(r)]
    exact_dupes = sum(n - 1 for n in Counter(map(tuple, nonblank)).values() if n > 1)
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

def profile_characters(name: str, values: list[str | None],
                       findings: list[Finding]) -> None:
    counts: Counter[str] = Counter()
    moji_examples: list[str] = []
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


def profile_nulls(name: str, values: list[str | None],
                  findings: list[Finding]) -> None:
    empties = sum(1 for v in values if v == "")
    ws_only = sum(1 for v in values if v is not None and v != "" and v.strip() == "")
    if empties:
        findings.append(finding("NUL-01", "ask" , f"{empties} empty value(s) — null or empty string?",
                                column=name, count=empties, group_key="empty-is-null"))
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
        # Group by the sentinel token set so identical sentinels across columns
        # (e.g. "(null)" everywhere) collapse to one confirmation.
        findings.append(finding("NUL-03", "ask",
                                "possible sentinel value(s) — confirm which mean null",
                                column=name, evidence=dict(hits.most_common(8)),
                                group_key="sentinel:" + ",".join(sorted(hits))))


def profile_types(name: str, values: list[str | None],
                  findings: list[Finding]) -> None:
    vals = [v.strip() for v in values if v is not None and v.strip()]
    vals = [v for v in vals if v.casefold() not in KNOWN_SENTINELS]
    if not vals:
        return
    n = len(vals)

    # TYP-06 boolean vocabulary. A single distinct value is not evidence of a
    # boolean column (corpus regression: 9k spurious findings on a wide file of
    # constant columns) — require two distinct values, or the X/blank checkbox
    # pattern where the blanks are the second state.
    lowered = {v.casefold() for v in vals}
    has_empties = any(v is not None and v.strip() == "" for v in values)
    if len(lowered) >= 2:
        for vocab in BOOL_VOCABS:
            if lowered <= vocab:
                findings.append(finding("TYP-06", "ask",
                                        f"boolean-like vocabulary {sorted(lowered)} — confirm truth mapping",
                                        column=name, group_key="bool:" + ",".join(sorted(lowered))))
                return
    elif lowered == {"x"} and has_empties:
        findings.append(finding("TYP-06", "ask",
                                "checkbox pattern (X/blank) — confirm truth mapping (true/false or true/null?)",
                                column=name, group_key="bool:x/blank"))
        return

    # TYP-01 formatted numerics
    fmt_counts: Counter[str] = Counter()
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

    # TYP-12 magnitude/scale-suffixed numerics (10.00K, 1.2M) — its own decision.
    mag = [v for v in vals if MAGNITUDE_RE.match(v)]
    if mag and len(mag) / n > 0.3:
        suffixes = Counter(v.strip()[-1].upper() for v in mag)
        findings.append(finding("TYP-12", "ask",
                                "magnitude/scale suffix(es) (e.g. K/M/B) — confirm SI scaling",
                                column=name, count=len(mag), evidence=dict(suffixes)))

    # TYP-07 numeric-looking identifiers
    digit_vals = [v for v in vals if v.isdigit()]
    if digit_vals and len(digit_vals) / n > 0.9:
        leading_zeros = sum(1 for v in digit_vals if LEADING_ZERO_RE.match(v))
        widths = {len(v) for v in digit_vals}
        if leading_zeros:
            findings.append(finding("TYP-07", "ask",
                                    "leading zeros present — keep as string to avoid data loss",
                                    column=name, count=leading_zeros,
                                    evidence=[v for v in digit_vals if LEADING_ZERO_RE.match(v)][:3],
                                    group_key="leading-zero"))
        elif len(widths) == 1 and widths.pop() >= 6 and len(set(digit_vals)) / len(digit_vals) > 0.9:
            findings.append(finding("TYP-07", "ask",
                                    "uniform-width, high-cardinality digits — identifier? keep as string",
                                    column=name, group_key="uniform-id"))

    # TYP-03 date format ambiguity
    slashy = [v for v in vals if DATE_SLASH_RE.match(v)]
    if slashy and len(slashy) / n > 0.6:
        mdy = dmy = both = bad = 0
        for v in slashy:
            # split each value on ITS OWN separator; a mixed-separator column
            # must not crash (F1) — non-3-part values just count as bad.
            parts = DATE_SPLIT_RE.split(v)
            if len(parts) != 3:
                bad += 1
                continue
            a, b = int(parts[0]), int(parts[1])
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
        groups: dict[str, set[str]] = {}
        for v in distinct:
            groups.setdefault(v.strip().casefold(), set()).add(v)
        collisions = {k: sorted(g) for k, g in groups.items() if len(g) > 1}
        if collisions:
            findings.append(finding("TYP-10", "ask",
                                    "values collide under trim+casefold — canonicalize?",
                                    column=name, evidence=collisions))


# ---------------------------------------------------------------------- interview plan

def group_findings(findings: list[Finding]) -> list[InterviewGroup]:
    """Collapse homogeneous per-column `ask` findings into one interview question
    each, so elicitation scales (a 9,074-column file must not yield 9,073 boolean
    questions). Findings sharing (id, group_key) are the SAME decision across
    columns and merge; every affected column is listed (never silently dropped).
    Findings without a group_key pass through as singleton groups."""
    groups: dict[tuple[str, str], InterviewGroup] = {}
    order: list[tuple[str, str]] = []
    for f in findings:
        if f["class"] != "ask":
            continue
        # Distinguish by group_key, else column, else the message itself — two
        # DIFFERENT same-id questions (e.g. duplicate-header vs blank-header
        # STR-04) must never collapse and drop one (F2). Never guess.
        key = (f["id"], f.get("group_key") or f.get("column") or f["message"])
        if key not in groups:
            groups[key] = {"id": f["id"], "class": "ask",
                           "group_key": f.get("group_key"),
                           "message": f["message"], "columns": [], "evidence": {}}
            order.append(key)
        g = groups[key]
        if f.get("column") and f["column"] not in g["columns"]:
            g["columns"].append(f["column"])
        if f.get("evidence"):
            g["evidence"][f.get("column") or "<dataset>"] = f["evidence"]
    out = []
    for key in order:
        g = groups[key]
        g["n_columns"] = len(g["columns"])
        if not g["evidence"]:
            del g["evidence"]
        out.append(g)
    return out


# ---------------------------------------------------------------------- main

def profile_file(path: str, max_rows: int = MAX_ROWS_DEFAULT) -> ProfileResult:
    findings: list[Finding] = []
    with open(path, "rb") as fh:
        raw = fh.read()
    encoding = profile_bytes(raw, findings)
    while raw.startswith(b"\xef\xbb\xbf"):  # strip every leading BOM (see profile_bytes)
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
        "interview_groups": group_findings(findings),
        "summary": {
            "ask": sum(1 for f in findings if f["class"] == "ask"),
            "ask_questions": len(group_findings(findings)),
            "fix": sum(1 for f in findings if f["class"] == "fix"),
            "row-error": sum(1 for f in findings if f["class"] == "row-error"),
        },
    }


def main() -> None:
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
    main()
