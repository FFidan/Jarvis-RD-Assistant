# Scientific RAG Model/Retrieval Eval Harness

This harness is for model and retrieval evidence. It is intentionally
separate from product defaults: a candidate can look promising here and still be
kept out of the default path until hardware, latency, structured-output, and
citation-grounding gates all pass.

## Inputs

- `docs/perf/eval_sets/2026-07-03-scientific-rag-eval.jsonl` contains fixed
  paper slots and question rows.
- `docs/perf/eval_sets/2026-07-03-scientific-rag-answer-key.jsonl` contains
  source-anchor checks, wrong-paper traps, quantitative checks, and unanswerable
  refusal conditions for judging.
- Judge-reviewed answer rows contain one `(candidate, question_id)` result with
  a visible answer, citations, 0-2 rubric scores, latency, VRAM metadata, and
  hard-fail flags.
- Raw traces, model outputs, timing CSVs, screenshots, and reviewer notes
  stay under ignored `artifacts/perf/<run-id>/`.

## Dry Run

Use dry run in CI or while editing the manifest. It validates coverage and report
aggregation without calling a model:

```bash
uv run python3 scripts/perf/llm_retrieval_eval.py   --dry-run   --candidate dry-run-fixture   --out-dir artifacts/perf/llm-retrieval-eval-dry-run
```

Dry-run output is not benchmark evidence and cannot promote a default.

## Seed Or Check The Fixed Pack

Before capture, verify that the authenticated benchmark library contains the
fixed 10-paper pack and no unrelated papers. The seeder writes only ignored
artifacts and accepts cookie/header files, not secret values on the command
line.

```bash
uv run python3 scripts/perf/seed_scientific_rag_pack.py \
  --manifest docs/perf/eval_sets/2026-07-03-scientific-rag-eval.jsonl \
  --api-base http://127.0.0.1:3001 \
  --auth-cookie-file artifacts/perf/<run-id>/jarvis-cookie.txt \
  --check-only \
  --out-dir artifacts/perf/<run-id>
```

If the readiness check reports missing paper rows, import the exact fixed-pack
arXiv identifiers inside the paper-ingestion service first; do not use broad
search/discovery routes for benchmark seeding. After the rows exist in the
authenticated library, use `--seed` only to download and process PDFs through
product-supported APIs, then repeat the check-only command:

```bash
uv run python3 scripts/perf/seed_scientific_rag_pack.py \
  --manifest docs/perf/eval_sets/2026-07-03-scientific-rag-eval.jsonl \
  --api-base http://127.0.0.1:3001 \
  --auth-cookie-file artifacts/perf/<run-id>/jarvis-cookie.txt \
  --seed \
  --out-dir artifacts/perf/<run-id>
```

## Capture Product RAG Answers

Capture mode calls the product RAG API and writes raw, unjudged rows. It does
not aggregate scores and it does not produce a benchmark decision. Use it on the
benchmark host only after the authenticated library contains the fixed benchmark
paper pack and no unrelated papers. Cross-paper and unanswerable questions use
the library-wide product route, so the harness fails closed unless
`--fixed-pack-library-confirmed` is supplied.

Create a local, ignored paper map from manifest keys to local database paper ids:

```json
{
  "p1_attention": 101,
  "p2_neural_ode": 102,
  "p3_lora": 103
}
```

Then capture one candidate at a time:

```bash
uv run python3 scripts/perf/llm_retrieval_eval.py \
  --manifest docs/perf/eval_sets/2026-07-03-scientific-rag-eval.jsonl \
  --capture-only \
  --api-base http://127.0.0.1:8000 \
  --candidate current-smart-local \
  --paper-map-json artifacts/perf/<run-id>/paper_map.json \
  --auth-cookie-file artifacts/perf/<run-id>/jarvis-cookie.txt \
  --fixed-pack-library-confirmed \
  --out-dir artifacts/perf/<run-id>
```

### vLLM Backend Candidate Route

For vLLM comparisons, start the vLLM server as a loopback-only backend and route
LiteLLM's `smart` alias through the runtime route table. Do not edit `litellm/config.yaml`;
that file no longer owns switchable smart/fast/fallback deployments. The vLLM
compose overlay sets `JARVIS_LITELLM_RECONCILER_ENABLED=false` for
`paper_ingestion` so the temporary runtime route is not overwritten during the
benchmark window; the default compose path keeps the reconciler enabled.

Create a restore snapshot before the temporary route, then route one candidate:

```bash
uv run python3 scripts/perf/litellm_route_alias.py route-openai \
  --alias smart \
  --served-model Qwen/Qwen2.5-7B-Instruct-AWQ \
  --api-base http://vllm:8080/v1 \
  --snapshot-out artifacts/perf/<run-id>/smart-route-before-vllm.json
```

After capture, restore the original deployment and verify route status:

```bash
uv run python3 scripts/perf/litellm_route_alias.py restore \
  --alias smart \
  --snapshot artifacts/perf/<run-id>/smart-route-before-vllm.json
uv run python3 scripts/perf/litellm_route_alias.py status --alias smart
```

Run the exact product capture command above after route status confirms `smart`
points at the intended vLLM deployment. Raw vLLM endpoint smoke is a readiness
check only; it is not benchmark evidence.

`--api-key` can be used instead of `--auth-cookie-file` only when the target
route accepts that authentication mode. Raw capture rows are written to
`artifacts/perf/<run-id>/raw_answers.jsonl` with `scores: null`; those rows are
intentionally rejected by aggregation until judged. Rows with missing judge
provenance, `judge_type: "model_self"`, missing non-empty source/citation data,
non-numeric latency/VRAM metadata, missing hard-fail booleans, or missing
fixed-pack scope confirmation for library-wide questions are also rejected.

## Raw Capture Gate

Before judging, run the raw gate over capture-only rows. This is a hard gate for
row completeness, fixed-pack scope, visible hidden-reasoning markers, non-200
responses, outside-corpus source ids, and latency/VRAM metadata. It writes a
JSON intake summary only; it does not score scientific quality and it does not
produce promotion decisions.

```bash
uv run python3 scripts/perf/llm_retrieval_eval.py \
  --manifest docs/perf/eval_sets/2026-07-03-scientific-rag-eval.jsonl \
  --raw-gate-jsonl artifacts/perf/<run-id>/raw_answers.jsonl \
  --paper-map-json artifacts/perf/<run-id>/paper_map.json \
  --vram-csv artifacts/perf/<run-id>/monitors/candidate-vram.csv \
  --out-dir artifacts/perf/<run-id>/raw-gate
```

`eligible_for_judging: true` means the raw capture is complete enough for
source-backed judging against the answer key. It is not benchmark evidence until
complete judged rows pass aggregation and independent review.

## Judge And Aggregate

1. Compare each raw row with the returned sources and the answer-key checks.
2. Create `artifacts/perf/<run-id>/judged_answers.jsonl` with one complete
   `scores` object per `(candidate, question_id)`, `judge_reviewed: true`,
   `judge_type: "executor"`, `"owner"`, or `"human"`, a visible `answer`, a
   non-empty `citations` or `sources` list, numeric `latency_ms`, numeric
   `vram_peak_mb`, and every hard-fail flag listed below. Preserve
   `retrieval_scope: "fixed_pack_isolated_library"` and
   `fixed_pack_library_confirmed: true` for cross-paper and unanswerable rows.
3. Preserve hard-fail flags for hidden reasoning, empty visible answers,
   wrong-paper central claims, fabricated unanswerables, and structured-output
   failures.
4. Aggregate judged rows only:

```bash
uv run python3 scripts/perf/llm_retrieval_eval.py \
  --manifest docs/perf/eval_sets/2026-07-03-scientific-rag-eval.jsonl \
  --answer-key docs/perf/eval_sets/2026-07-03-scientific-rag-answer-key.jsonl \
  --answers-jsonl artifacts/perf/<run-id>/judged_answers.jsonl \
  --route-label smart \
  --backend-label ollama \
  --runtime-inventory artifacts/perf/<run-id>/runtime_inventory.json \
  --out-dir artifacts/perf/<run-id>/summary
```

## Real Run Workflow

1. Record runtime inventory: git commit, OS, GPU/VRAM, Docker image tags,
   LiteLLM aliases, Ollama models, configured smart/fast/embed routes, reranker
   settings, and product-Ollama reachability.
2. Seed or confirm the local benchmark library contains only the fixed paper
   corpus before running library-wide capture.
3. Capture answers for the current baseline before candidates.
4. Judge captured rows against returned sources and the answer-key packet.
5. Aggregate only complete judged rows with manifest, answer-key, answer-row,
   git commit, route/backend label, and runtime-inventory metadata.
6. Roll back any temporary model, reranker, backend, or environment override
   before normal product use.

Candidate comparisons should run behind existing aliases or explicit temporary
overrides. Cloud providers are out of scope for default promotion and are only
optional BYOK comparison inputs if the owner asks for that comparison.

## Hard-Fail Rules

Do not promote a default if any of these occur:

- Visible `<think>`, hidden reasoning, Harmony analysis, or scratchpad content.
- More than one empty visible answer across the fixed 24 questions.
- More than two central wrong-paper citations.
- Any unanswerable question gets a fabricated positive claim.
- Structured-output failure rate exceeds the current baseline.
- p95 latency or VRAM is unusable for the intended hardware tier.

## Artifact And Publication Boundary

Raw capture output, cookies, local paper ids, long source excerpts, logs, and
reviewer notes stay under ignored `artifacts/perf/<run-id>/`. Tracked
summary files under `docs/perf/` are allowed only when they contain measured
rows or explicit `not_runnable:<reason>` values.

The checked-in manifest and operator instructions may become public if the
resulting report is reproducible, source-backed, and free of unverifiable
scoring prose. Do not add a MkDocs/public link until an independent maintainer review
checks reproducibility, source-backed scoring, and artifact hygiene.
