# Per-cell judge prompt

You are scoring one (tier, candidate-model) cell from a research-paper RAG eval.

## Inputs

- **Seed papers (ground truth):** the 5 papers in `${SEED_DIR}` (full text)
- **Prompts:** `scripts/perf/quality/prompts.jsonl` (12 prompts, ids 1-12)
- **Candidate answers:** `${CELL_DIR}/q01.txt` through `q12.txt`

## Rubric (per prompt, score each dimension 0-2)

1. **accuracy-vs-source** — claims in the answer are supported by the seed papers
2. **citation-grounding** — answer cites specific papers / sections when claiming
3. **math-correctness** — for math prompts (#6, #7), correct equations/derivations; for non-math, score 2 by default
4. **hallucination-absence** — no fabricated claims, models, methods, or numbers
5. **completeness** — addresses what the prompt asked
6. **conciseness** — no padding, no `<think>` blocks, no truncation mid-answer

Per-prompt max = 12. Per-cell aggregate = sum over 12 prompts (max 144).

## Output

Write `${CELL_DIR}/scores.md` with:

```
# Cell: <tier> / <model>
Aggregate: <0-144>

## q01 (capability: methodology)
- accuracy-vs-source: 2 — answer matches paper X §3
- citation-grounding: 1 — references paper but not section
- math-correctness: 2 — N/A
- hallucination-absence: 2 — no fabricated claims
- completeness: 2 — fully addresses
- conciseness: 1 — slightly verbose
Subtotal: 10/12

[repeat for q02..q12]
```

Then write a single `${CELL_DIR}/verdict.txt` line:
`<tier>\t<model>\t<aggregate>\t<one_line_summary>`
