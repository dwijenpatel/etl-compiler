#!/usr/bin/env python3
"""audit.py — sweep the profiler across corpus/raw and aggregate what it sees.

Outputs (corpus/audit-out/, gitignored):
  per-file/<mirrored-path>.json   profiler findings per file
  audit-summary.json              histogram of taxonomy IDs, crash list, silent files,
                                  per-file finding counts

The three audit signals (see docs/taxonomy.md § Validation Plan):
  - crashes: the profiler couldn't even process the file -> structural gap or bug
  - silent files: zero findings -> either genuinely clean or a coverage gap (candidates
    for blind review)
  - ID histogram: which taxonomy entries fire in the wild, which never do

Usage: python3 corpus/audit.py [--max-rows 1000]
"""
import argparse
import importlib.util
import json
import os
import sys
import traceback
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
OUT = os.path.join(HERE, "audit-out")
PROFILER = os.path.join(HERE, "..", "skill", "etl-generator", "scripts", "profile.py")

EXTS = {".csv", ".tsv", ".txt"}


def load_profiler():
    spec = importlib.util.spec_from_file_location("profile_mod", PROFILER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-rows", type=int, default=1000)
    args = ap.parse_args()

    prof = load_profiler()
    per_file_dir = os.path.join(OUT, "per-file")
    os.makedirs(per_file_dir, exist_ok=True)

    id_hist = Counter()          # taxonomy ID -> total finding count
    id_files = Counter()         # taxonomy ID -> number of files it fired in
    crashes = []
    silent = []
    per_file_counts = {}
    n_files = 0

    for root, _, files in os.walk(RAW):
        for fname in sorted(files):
            if os.path.splitext(fname)[1].lower() not in EXTS:
                continue
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, RAW)
            n_files += 1
            try:
                result = prof.profile_file(path, args.max_rows)
            except Exception:
                crashes.append({"file": rel, "traceback":
                                traceback.format_exc(limit=3).splitlines()[-1]})
                continue
            findings = result.get("findings", [])
            ids_here = sorted({f["id"] for f in findings})
            for f in findings:
                id_hist[f["id"]] += 1
            for fid in ids_here:
                id_files[fid] += 1
            per_file_counts[rel] = {"findings": len(findings), "ids": ids_here}
            if not findings:
                silent.append(rel)
            out_path = os.path.join(per_file_dir, rel.replace(os.sep, "__") + ".json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=1, ensure_ascii=False)

    summary = {
        "files_profiled": n_files,
        "crashes": crashes,
        "silent_files": silent,
        "id_histogram_findings": dict(id_hist.most_common()),
        "id_histogram_files": dict(id_files.most_common()),
        "per_file": per_file_counts,
    }
    with open(os.path.join(OUT, "audit-summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1, ensure_ascii=False)

    print(f"files profiled: {n_files}  crashes: {len(crashes)}  silent: {len(silent)}")
    print("\nID histogram (files it fired in / total findings):")
    for fid, nf in id_files.most_common():
        print(f"  {fid:8s} {nf:4d} files  {id_hist[fid]:5d} findings")
    if crashes:
        print("\ncrashes:")
        for c in crashes[:10]:
            print(f"  {c['file']}: {c['traceback']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
