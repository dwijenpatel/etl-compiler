# Eval Iteration 2 — Benchmark

Runtime v0.2.0 · taxonomy v0.2 · contract-level assertions · isolated baselines · Opus 4.8 runs.

| Eval | With skill | Baseline | Notes |
|---|---|---|---|
| 0 · messy-csv-unattended | 8/8 | 8/8 | skill KEEPS dup, baseline DROPS (both recorded); skill uses taxonomy codes |
| 1 · ambiguous-dates | 6/6 | 6/6 | skill quarantines all 8; baseline loads 7 under assumed MDY + flags |
| 2 · regen-from-spec | 5/5 | 5/5 | skill 89.7s < baseline 119.2s (composability) |
| **Total** | **19/19** | **19/19** | tie — see below |

| Aggregate | With skill | Baseline |
|---|---|---|
| Assertions | 19/19 (100%) | 19/19 (100%) |
| Mean time | 351.9s | 200.1s |
| Mean tokens | 91,158 | 53,775 |

**Finding:** the hard contract-level assertions do not separate the arms — a strong isolated
baseline meets the contract unaided. The skill's value is in what a single run can't show
(determinism, stable codes, tested runtime, editable spec). Full analysis:
[docs/eval-report-iteration-2.md](../../docs/eval-report-iteration-2.md). Iteration-3 should
measure determinism/consistency, not single-run correctness.
