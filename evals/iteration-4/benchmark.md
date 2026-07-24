# Eval Iteration 4 — Determinism Regression on the New Stack

Iteration 3 proved the four determinism properties (skill 4/4 · baseline 0/4) on
runtime 0.2.0 / compiler 0.1.0 / taxonomy 0.2. The stack has since changed
substantially — two-file runtime **0.7.0**, compiler **0.5.0** (full 17-trap
vocabulary, annotate disposition, ENC-08 formula policy), taxonomy **0.4**, spec
format **0.4**, compiler-first SKILL.md. Iteration 4 re-verifies the same four
properties AND tests the three iteration-3 honest limits we claimed to close.

**Result: 4/4 properties still hold · 3/3 honest limits confirmed closed · 2/2
new capabilities behave as specified.**

Baselines were deliberately **not rerun**: baseline arms never touch the
skill/runtime/compiler, so the stack changes cannot alter their behavior;
iteration-3's baseline measurements (0/4) remain the valid comparison.

| Property | Iteration 3 (old stack) | Iteration 4 (current stack) |
|---|---|---|
| **E1 · Regenerate same spec ×3** | 3/3 byte-identical (vendor spec only — 17-trap spec was *outside* compiler vocabulary) | **3/3 byte-identical for BOTH specs**, incl. the 17-trap spec, pipelines and run artifacts |
| **E2 · Author from messy file ×3** | decisions + outputs identical, but 2/3 hand-generated, names varied, ENC-06 repair uncounted | decisions **3/3** · outputs **3/3** · summaries **3/3** (identical warning maps) · **compiler-generated 3/3** · **names identical 3/3** · **ENC-06 counted 3/3** |
| **E3 · Unseen-problem variants ×4** | 4/4 exact taxonomy codes | **4/4 exact codes**, accounting reconciles, raw preserved |
| **E4 · One-decision edit** | 1-line diff; wrong edit fails loud (ERR-02) | same — plus two NEW edits: **annotate** (1 policy line → load + `changes.jsonl` ledger, exit 2) and **formula neutralize** (spec 0.4 → `'`-prefixed cells, ENC-08 counted, signed numerics untouched) |
| **E5 · New detections** (new) | — | 8/8: ENC-08 + TYP-04 asks fire where planted; no false positives on signed numerics, version numbers, or "Totally Organic" |

**Honest notes.** Independent authors still don't produce byte-identical *specs*
(comment/evidence wording differs), so cross-author pipelines differ in
sha-header/comments — not claimed, and E1 shows identical spec bytes → identical
everything. One residual metadata wobble: the duplicate decision's provenance
label varied (`default` vs `unconfirmed` ×2) — same decision, same behavior;
SKILL.md's unattended rule favors `unconfirmed`, a one-line tightening candidate.
One run initially omitted the footer skip-rule, saw the footer surface in
quarantine as NUL-04, and self-corrected before delivering — the reports made the
misconfiguration visible, which is the design working.

Full data: [results.json](results.json) · methodology: [eval_metadata.json](eval_metadata.json) ·
graders: `grade_e1.py`, `grade_e2.py` · artifacts under `e1-regen/`, `e2-authoring/`,
`e3-variants/`, `e4-edit/`, `e5-new-detections/`.
