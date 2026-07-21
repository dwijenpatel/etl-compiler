# Eval Report — Iteration 2

**Date:** 2026-07-20
**Runtime/taxonomy:** v0.2.0 / v0.2 (profiler fixes + interview batching + runtime hardening)
**Setup:** the 3 iteration-1 tasks, rerun with **contract-level** assertions (drafted in the
iteration-1 report), each task run once per arm by an independent Opus 4.8 subagent — with
the `etl-generator` skill vs baseline. **Baseline isolation fixed this iteration** (explicit
"do not read/import from the repo"); an iteration-2 baseline had otherwise copied the skill
runtime verbatim. Artifacts in `evals/iteration-2/` (per-run `outputs/`, `grading.json`,
`timing.json`, `benchmark.json`).

## Results

| Metric | With skill | Baseline | Delta |
|---|---|---|---|
| Assertions passed | **19/19 (100%)** | **19/19 (100%)** | 0 |
| Mean time | 351.9s | 200.1s | +151.8s |
| Mean tokens | 91,158 | 53,775 | +37,383 |

Per-eval: eval-0 8/8 vs 8/8 · eval-1 6/6 vs 6/6 · eval-2 5/5 vs 5/5. On eval-2 (regen from
an existing spec), with-skill was again **faster** than baseline (89.7s vs 119.2s) — the
composability payoff repeats from iteration-1.

## The headline finding (deflationary, and the point of the exercise)

**The redesigned contract-level assertions still do not separate skill from baseline — and
this iteration proves it wasn't an artifact of weak assertions.** Iteration-1's assertions
were partly filler (runs-to-completion, report-exists). Iteration-2's are the hard ones:
machine-readable accounting completeness, duplicates-not-silently-dropped, quarantine
reprocessability, repair-is-opt-in-and-flagged. **A strong, properly isolated Opus baseline
passes all of them unaided** — it produced reconciling counts, a per-fix tally, a rejects
file with raw rows, and a `review_required` list flagging the mojibake repair, the dropped
duplicate, and the ambiguous-date assumption.

At this model tier, single-run output quality is not where the skill earns its keep. The
model writes contract-compliant ETL on its own. What a single graded run **cannot** show is
exactly where the skill's value lives:

1. **Determinism / same-spec→same-behavior.** The baseline's dispositions are per-run
   judgment; the skill's are encoded in the spec and regenerate byte-identically. Two
   baseline runs of the same messy file can diverge; two skill runs cannot.
2. **Stable, aggregatable error codes.** The skill emits taxonomy IDs (`NUL-04`, `STR-02`,
   `TYP-03`); the baselines invented ad-hoc strings (`MISSING_REQUIRED`). Cross-pipeline
   monitoring needs the former.
3. **A tested shared runtime.** The baseline reimplements edge-case semantics each time,
   untested; the skill imports one runtime with a 55-test suite.
4. **The spec as a reviewable, editable artifact** — the unit of change and audit.

## Real divergences the assertions gave equal credit to (these matter more than the score)

- **Duplicate disposition (eval-0).** Skill **keeps** the exact-duplicate row and reports it
  (taxonomy default: a repeated order may be legitimate); the baseline **drops** it (primary-
  key framing). Both recorded it machine-readably — so both pass — but the semantics are
  opposite, and a silent drop (iteration-1's baseline) would now fail assertion 2.
- **Ambiguous-date disposition (eval-1).** Skill **quarantines all 8 rows** (nothing
  reinterpreted, everything reprocessable); baseline **loads 7** under an assumed MDY format
  and flags them. Both "don't silently guess"; opposite dispositions. The skill's
  quarantine-everything is the more purist reading of the never-guess rule — arguably *too*
  conservative for some unattended contexts (0 rows loaded), which connects to the ERR-01
  disposition analysis (`docs/taxonomy-validation-report.md` + the evidence corpus).

## What iteration-2 actually delivered

- The assertions now **verify the contract is met** (not just "it ran") — that is the real
  upgrade over iteration-1, even though the score is tied.
- **Baseline isolation is fixed**, so the comparison is now honest (both v0.2 baselines
  wrote their own runtimes; sha-distinct from the skill's).
- The runtime hardening shows up: the with-skill manifests now carry `spec_sha256` +
  `generator_version` (ERR-06 completeness), duplicates are counted (STR-05), trims are
  counted (TYP-10) — machinery the baselines mostly don't replicate.

## Recommendation for iteration 3

Stop measuring single-run correctness — the model wins that unaided. Measure the properties
only the skill provides:

1. **Determinism harness:** run the same spec N times; assert byte-identical output +
   reports. Baseline (no spec) will vary; skill will not. *This is the skill's actual moat
   and no current eval tests it.*
2. **Consistency-across-inputs:** same spec, several messy variants of the input; assert
   error codes and dispositions stay stable. Baselines re-improvise per input.
3. **Spec-edit propagation:** change one decision in the spec, regenerate, assert exactly
   that behavior changed and nothing else.
4. Keep the contract assertions as a **floor** (they'd catch a regression), not as the
   differentiator.
