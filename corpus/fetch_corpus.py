#!/usr/bin/env python3
"""fetch_corpus.py — reproducibly fetch the messy-data corpus described by manifest.json.

Two entry kinds:
  http: {"id", "kind": "http", "url", "dest", "license", "note"}
  git:  {"id", "kind": "git", "repo", "ref", "paths": [globs], "dest", "license", "note"}

Files land under corpus/raw/<dest>/ (gitignored). After fetching, each entry's
observed sha256 set is written back to manifest.lock.json so drift is detectable.

Usage: python3 corpus/fetch_corpus.py [--only ID] [--refresh]
Stdlib + git + curl only.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
MANIFEST = os.path.join(HERE, "manifest.json")
LOCK = os.path.join(HERE, "manifest.lock.json")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_http(entry, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    fname = entry.get("filename") or entry["url"].rstrip("/").split("/")[-1].split("?")[0]
    out = os.path.join(dest_dir, fname)
    subprocess.run(["curl", "-fsSL", "--max-time", "120", "-o", out, entry["url"]],
                   check=True)
    return [out]


def fetch_git(entry, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="corpusgit-")
    try:
        subprocess.run(["git", "clone", "--quiet", "--depth", "1",
                        *(["--branch", entry["ref"]] if entry.get("ref") else []),
                        entry["repo"], tmp], check=True)
        head = subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        entry["pinned_sha"] = head
        copied = []
        for root, _, files in os.walk(tmp):
            if ".git" in root.split(os.sep):
                continue
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), tmp)
                if any(fnmatch.fnmatch(rel, g) for g in entry["paths"]):
                    out = os.path.join(dest_dir, rel.replace(os.sep, "__"))
                    shutil.copy2(os.path.join(root, f), out)
                    copied.append(out)
        return copied
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", help="fetch a single manifest id")
    ap.add_argument("--refresh", action="store_true", help="re-fetch even if dest exists")
    args = ap.parse_args()

    with open(MANIFEST, encoding="utf-8") as f:
        manifest = json.load(f)
    lock = {}
    failures = []
    for entry in manifest["entries"]:
        if args.only and entry["id"] != args.only:
            continue
        dest_dir = os.path.join(RAW, entry["dest"])
        if os.path.isdir(dest_dir) and os.listdir(dest_dir) and not args.refresh:
            files = [os.path.join(dest_dir, f) for f in sorted(os.listdir(dest_dir))]
            print(f"[skip] {entry['id']} (present, {len(files)} file(s))")
        else:
            try:
                files = (fetch_http if entry["kind"] == "http" else fetch_git)(entry, dest_dir)
                print(f"[ok]   {entry['id']} ({len(files)} file(s))")
            except subprocess.CalledProcessError as e:
                print(f"[FAIL] {entry['id']}: {e}", file=sys.stderr)
                failures.append(entry["id"])
                continue
        lock[entry["id"]] = {
            "pinned_sha": entry.get("pinned_sha"),
            "files": {os.path.relpath(p, RAW): {"sha256": sha256(p),
                                                "bytes": os.path.getsize(p)}
                      for p in files},
        }
    with open(LOCK, "w", encoding="utf-8") as f:
        json.dump(lock, f, indent=1, sort_keys=True)
    print(f"\n{len(lock)} entr(ies) fetched; lock written to {os.path.relpath(LOCK)}")
    if failures:
        print(f"FAILED: {failures}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
