# Eval Iteration 3 — Determinism Benchmark

The first iteration where the arms separate. Iterations 1–2 measured single-run
correctness (tie: a strong model meets the contract unaided). Iteration 3 measures the
four properties the skill actually claims. **Skill 4/4 · baseline 0/4.**

| Property | With skill | Without skill |
|---|---|---|
| **E1 · Regenerate same spec ×3** | pipelines + outputs **3/3 byte-identical** (compiler, no model) | 0/3 pipelines identical (232/454/232 lines); **values 3/3** (the spec pins data) but 0/3 report schemas/filenames/exit conventions |
| **E2 · Author from messy file ×3** | outputs + summaries **3/3 identical**; dispositions identical (quarantine ragged, keep dup, flag repair) | **0/3 outputs identical** (7/6/7 rows); dup kept/**dropped**/kept; ragged **padded 3/3**; JSON report present 1/3 |
| **E3 · Unseen-problem variants ×4** | **4/4** correct taxonomy codes, accounting reconciles | handled, but codes ad-hoc / prose / **2 of 4 misassigned** (ragged→"STR-01", overflow→"STR-07") |
| **E4 · One-decision edit** | diff = **exactly 1 line** (+hash header); wrong-for-data edit **fails loud** (ERR-02) | clean 5-line edit, but its improvised runtime reads the same budget spec oppositely (completes where the runtime aborts) |

**Honest limits:** compiler vocabulary bounds the mechanical guarantee — 2/3 authoring
runs hand-generated (spec needed ENC-06 repair + STR-06 footer-skip ops); expr-based
repair isn't warning-counted yet. Both filed as compiler/runtime backlog with two
agent-produced upstream candidates (`repair_mojibake`, `skip_if`).

Full data: [results.json](results.json) · per-arm artifacts under `e1-regen/`,
`e2-authoring/`, `e3-variants/`, `e4-edit/`.
