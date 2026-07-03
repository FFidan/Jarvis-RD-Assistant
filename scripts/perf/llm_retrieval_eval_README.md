# Scientific RAG Model/Retrieval Eval Harness

This harness is for Step 0.5 model/retrieval evidence. It is intentionally
separate from product defaults: a candidate can look promising here and still be
kept out of the default path until hardware, latency, structured-output, and
citation-grounding gates all pass.

## Inputs

- `docs/perf/eval_sets/2026-07-03-scientific-rag-eval.jsonl` contains fixed
  paper slots and question rows.
- Judge-reviewed answer rows contain one `(candidate, question_id)` result with
  a visible answer, citations, 0-2 rubric scores, latency, VRAM metadata, and
  hard-fail flags.
- Raw traces, model outputs, timing CSVs, screenshots, and scratch judging notes
  stay under ignored `artifacts/perf/<run-id>/` or `docs/audit/exec/<run-id>/`.

## Dry Run

Use dry run in CI or while editing the manifest. It validates coverage and report
aggregation without calling a model:

```bash
uv run python3 scripts/perf/llm_retrieval_eval.py   --dry-run   --candidate dry-run-fixture   --out-dir artifacts/perf/llm-retrieval-eval-dry-run
```

Dry-run output is not benchmark evidence and cannot promote a default.

## Real Run Workflow

1. Seed or confirm the fixed paper corpus in the local JARVIS library.
2. Capture answers for the current baseline before candidates.
3. Score each visible answer with the rubric in the implementation plan.
4. Save judged answer rows as JSONL under `artifacts/perf/<run-id>/answers.jsonl`.
5. Aggregate:

```bash
uv run python3 scripts/perf/llm_retrieval_eval.py   --answers-jsonl artifacts/perf/<run-id>/answers.jsonl   --out-dir artifacts/perf/<run-id>/summary
```

## Hard-Fail Rules

Do not promote a default if any of these occur:

- Visible `<think>`, hidden reasoning, Harmony analysis, or scratchpad content.
- More than one empty visible answer across the fixed 24 questions.
- More than two central wrong-paper citations.
- Any unanswerable question gets a fabricated positive claim.
- Structured-output failure rate exceeds the current baseline.
- p95 latency or VRAM is unusable for the intended hardware tier.

## Publication Boundary

The checked-in manifest and operator instructions may become public if the
resulting report is reproducible and free of subjective LLM-judge prose. Raw
outputs and local operator traces remain internal unless deliberately sanitized.
