# Eval Report — Iteration 1

**Date:** 2026-07-17/18 (founding session)
**Setup:** 3 eval tasks, each run by an independent subagent twice — once with the `etl-generator` skill, once without (baseline) — then graded against programmatic assertions. Full artifacts in `evals/iteration-1/` (per-run `outputs/`, `grading.json`, `benchmark.json`, browsable `review.html`).

## Tasks

| # | Name | Tests |
|---|---|---|
| 0 | messy-csv-unattended-transform | 17-trap CSV → target format, unattended (BOM, NFD unicode, control char, NBSP, mojibake, $/parens numerics, MDY dates, Y/N, leading-zero ZIPs, N/A sentinel, ragged/dup/blank/footer rows, mixed line endings) |
| 1 | ambiguous-dates-no-guessing | Feed where every date parses as both MDY and DMY; 9999 sentinels; carrier case collisions; empty NOT NULL id — unattended |
| 2 | regen-from-existing-spec | Regenerate pipeline from a provided `.etlspec.yaml` without changing decisions |

## Results

| Metric | With skill | Baseline | Delta |
|---|---|---|---|
| Assertions passed | 20/20 (100%) | 19/20 (95%) | +1 |
| Mean time | 290.6s | 168.0s | +122.6s |
| Mean tokens | 80.7k | 47.8k | +32.9k |

Per-eval: eval-0 7/7 vs 6/7 · eval-1 7/7 vs 7/7 · eval-2 6/6 vs 6/6. On eval-2 (regen), with-skill was **faster** than baseline (85s vs 145s) at equal tokens — regeneration over a known runtime is mostly mechanical. The skill's cost concentrates where it does profiling + spec authoring, which is what buys the auditable artifact.

## The one baseline failure (and why it's the archetypal one)

Baseline eval-0 **padded** the ragged row into the output (inventing an empty `notes` value) instead of quarantining. Its prose notes disclosed this honestly — but its machine-readable report (`dropped_rows` / `quarantined_rows` / `warnings`) omits the row entirely. Prose honest, telemetry wrong. The skill quarantined the same row with a coded record (`STR-02`, "expected 7 fields, got 6") and preserved the raw row.

## Non-asserted divergences (found by manual review — these matter most)

1. Baseline silently **dropped the exact-duplicate row** (taxonomy default: keep-and-report — a legitimate repeated order would be destroyed).
2. Baseline **auto-repaired mojibake** by default (taxonomy: repair is opt-in, ENC-06, because heuristic repair on non-mojibake corrupts data). Note: the target sample in eval-0 shows repaired names, so the with-skill run also repaired — but recorded it as an `unconfirmed` decision requiring review, which is the designed behavior.
3. Eval-1 disposition difference: skill **quarantined** ambiguous-date rows (reprocessable — raw rows preserved); baseline loaded them with **NULL dates + annotations** (downstream sees rows that "loaded fine" with missing dates). Both "didn't guess," but the semantics differ.

## Assertion quality critique

- Several assertions were non-discriminating (runs-to-completion, report-exists — always pass). Filler.
- Eval-1's assertion set gave identical credit to quarantine-vs-null-annotate (see above).
- The never-guess-dates behavior appears model-innate at this model tier — the skill's value there is the *spec artifact* (`provenance: unconfirmed`, `review_required`, one-line edit to unblock), not the refusal itself.

## Iteration 2 plan (drafted, not yet run)

New contract-level assertions:
1. **Accounting completeness:** every row that was modified, dropped, padded, or excluded appears in the machine-readable report with a reason code — verified by reconciling rows_in against output + quarantine + skips, and by cross-checking each planted trap row's disposition.
2. **Duplicate policy:** exact duplicates are kept and reported (or their removal is an explicit, recorded decision) — never silently dropped.
3. **Quarantine reprocessability:** quarantined raw rows are preserved intact and a documented re-run path exists.
4. **Repair opt-in:** mojibake (and any content-changing repair) is either off or explicitly recorded as a reviewable decision.
5. Keep: leading-zero preservation, accounting negatives, sentinel handling, NOT NULL rejection.
6. Drop: runs-to-completion and report-exists as standalone assertions (fold into #1).

Process: rerun all 3 evals (+ baselines) into `evals/iteration-2/`, aggregate with skill-creator's `aggregate_benchmark`, generate the review page with `--previous-workspace evals/iteration-1`, collect human feedback, revise SKILL.md accordingly. Also pending: user feedback from the iteration-1 `review.html` was not yet returned when the founding session ended — check whether a `feedback.json` exists before planning changes.
