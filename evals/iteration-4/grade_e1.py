#!/usr/bin/env python3
"""E1 grader: pairwise byte-identity of pipelines + run artifacts across 3 regens."""
import itertools
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))


def grade(spec: str) -> dict:
    runs = [os.path.join(BASE, "e1-regen", spec, f"run-{i}") for i in (1, 2, 3)]
    pipe_ok = art_ok = mani_ok = 0
    pairs = list(itertools.combinations(runs, 2))
    for a, b in pairs:
        pa = open(os.path.join(a, f"{spec}_pipeline.py"), "rb").read()
        pb = open(os.path.join(b, f"{spec}_pipeline.py"), "rb").read()
        pipe_ok += pa == pb
        same = all(
            open(os.path.join(a, "etl_out", f), "rb").read()
            == open(os.path.join(b, "etl_out", f), "rb").read()
            for f in ("output.csv", "errors.jsonl", "summary.json"))
        art_ok += same
        ma = json.load(open(os.path.join(a, "etl_out", "manifest.json")))
        mb = json.load(open(os.path.join(b, "etl_out", "manifest.json")))
        ma.pop("completed_at_utc"), mb.pop("completed_at_utc")
        mani_ok += ma == mb
    n = len(pairs)
    return {"pipeline_pairwise_byte_identity": f"{pipe_ok}/{n}",
            "run_artifact_pairwise_byte_identity": f"{art_ok}/{n}",
            "manifest_identity_modulo_timestamp": f"{mani_ok}/{n}"}


if __name__ == "__main__":
    out = {spec: grade(spec) for spec in ("vendor_orders", "orders_export")}
    print(json.dumps(out, indent=2))
    ok = all(v.endswith("3/3") or v == "3/3" for d in out.values() for v in d.values())
    sys.exit(0 if ok else 1)
