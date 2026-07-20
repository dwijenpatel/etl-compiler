# Brainstorm Log — How etl-solved Got Its Shape

Distilled from the founding session (2026-07-17, Dwijen + Claude). Recorded so future sessions understand not just *what* was decided but *why*, and which alternatives were considered and set aside.

## Origin

Started as a PRD for an "AI-enabled ETL code generator": provide sample input/output tables, AI proposes a mapping, user refines interactively (chat or manual), code is generated (Python/TypeScript). Initial scope decisions: flat tabular only; standalone web app built in composable pieces with a CLI; user runs the generated code themselves; optional schema ingestion added in v1.1 (schema as source of truth when present, samples fill gaps, provenance-tagged facts).

## The stress-test (key tensions surfaced)

1. **The real competitor is a chat window.** For one-off transforms, pasting samples into any strong LLM produces decent code. The product's defense is the *spec* — structured review, deterministic regeneration, auditable decisions — which only earns its keep when mappings are long-lived, numerous, or trusted by someone other than the author.
2. **Three personas is two too many.** "User runs the code" quietly excludes analysts (nowhere to run Python); senior data engineers can write the boilerplate themselves. The underserved middle: people who repeatedly onboard messy external data (vendor/partner feeds).
3. **Distribution beats features (the FlashFill lesson).** Programming-by-example tools win by living where the data already is. A web app asks users to leave their environment; a Claude Code skill meets engineers in it.

## The pivot

Dwijen: money is explicitly NOT the goal. The motivation is that ETL failure modes are *tractable, generically solvable problems* — weird characters, nulls, empty fields, error/warning fidelity (per-record vs per-run, aggregate by type vs by row) — and he wants the workflow to exist: describe source/destination, interactively refine transformation + error/null handling, align on details, get correct conformant code.

This reframed everything:

- **The taxonomy is the center of gravity, not the AI.** What was described is a catalog of failure modes with finite decision spaces, configured through conversation. The AI is the interface; the asset is the taxonomy. Checklists compile.
- **The interview is structured, not freeform.** Profiling detects which failure modes are actually present; the interview asks only about those (`ask`-class); documented house defaults cover the rest; every decision — including defaults — lands in the spec with provenance.
- **The killer demo is the error report**, not the mapping proposal: feed the pipeline a filthy file, get quarantined rows aggregated by error type with row numbers and offending values. One-shot LLM code never has a coherent error-reporting architecture; that's the differentiator, because *consistency is precisely what one-shot generation cannot offer*.

## Decision: "Option Two" (runtime library over inlined codegen)

Considered: (1) fully standalone generated code with all hardened handling inlined — portable, but every pipeline is a copy and fixes don't propagate; (2) thin generated orchestration over a small, well-tested shared runtime — fixes propagate, tests accumulate in one place, "conformant" is enforced by tested code. **Chose (2), explicitly.** Zero-dependency inlined output may return as a much-later export feature (compiler inlines from tested, versioned templates).

## Decision: skill-first

Sequence: Claude Code skill + shared runtime now (cheapest test of the riskiest assumption — do people want spec-mediated, taxonomy-driven generation vs. just asking for a script?); the spec format is the durable asset; a web/analyst surface can come later on the same core. The analyst answer, when its time comes: analysts *author specs* (the spec is the executable replacement for the source-to-target mapping spreadsheet) and their codegen target is SQL/dbt — change the target, not the user.

## What the first eval iteration taught (2026-07-18)

Baseline Claude is already good at ETL mechanics — it even refuses to guess ambiguous dates. The skill's measurable edge is **contract-level**: quarantine-don't-pad (baseline padded a ragged row and its machine-readable report omitted the row entirely, while its prose notes were honest — telemetry lied, prose didn't); keep-duplicates-by-default (baseline silently dropped a duplicate order row); repair-as-opt-in (baseline auto-repaired mojibake). Also: regeneration-from-spec was *faster* with the skill than baseline (85s vs 145s) — the composability payoff is real. Iteration 2 should assert the contract, not the mechanics.
