# Phase D — Scientific Retrieval Upgrade

**Status:** Evaluation harness shipped; Qwen3-Embedding-4B default promoted
**Date:** 2026-05-06
**Depends on:** Phase C embedding migration completion (satisfied locally on 2026-05-06)

## Purpose

Phase D tracks retrieval quality improvements for math-heavy and notation-heavy
scientific papers after the Qwen3-Embedding-0.6B migration. The 2026-05-11
closeout promoted Qwen3-Embedding-4B as the local default because the repo now
treats scientific/notation-heavy papers as the primary workload. The remaining
Phase D work is evaluation quality, reranker comparison, and live parity proof
on each machine before claiming retrieval quality gains.

## Decisions

- Keep `qwen3-embedding:0.6b` as the explicit smaller-machine fallback at 1024
  dimensions.
- Use `qwen3-embedding:4b` as the local default at 2560 dimensions for
  notation-heavy scientific retrieval. Existing 1024d installs must take a
  Qdrant snapshot and run the guarded re-embed path before serving mixed-model
  vectors.
- Evaluate Qwen3 rerankers against the current production reranker,
  `mixedbread-ai/mxbai-rerank-base-v2`. The older claim that production still
  used `cross-encoder/ms-marco-MiniLM-L-6-v2` is stale for this checkout.
- Prefer local models. Cloud reasoning models and cloud embedding providers stay
  future-only unless the user explicitly opts into cloud operation.
- Keep evaluation honest: failed embedding/search queries stay in the metric
  denominator rather than being silently dropped.
- Use ONNX acceleration only when the installed `onnxruntime` package exposes
  the matching execution provider. CPU fallback is acceptable; claiming
  ONNX/CUDA without `CUDAExecutionProvider` is not.

## Evaluation Plan

1. Phase C live re-embedding and PG/Qdrant parity were completed locally at
   1024d/`qwen3-embedding:0.6b`. The current default is now
   2560d/`qwen3-embedding:4b`; each machine must re-run the parity proof after
   its guarded re-embed.
2. `scripts/eval_retrieval.py` can now run either from verified DB findings or
   a fixed JSONL eval set via `EVAL_RETRIEVAL_SET=/path/to/eval.jsonl`.
   The fixed-set schema accepts:
   `{"query": "...", "expected_paper_ids": [123], "tags": ["math"]}`.
3. Baseline metrics include Precision@1, Recall@3, nDCG@3, average latency,
   total query count, and failed query count.
4. Create a durable fixed scientific retrieval eval set from verified papers and
   queries that include equations, symbols, abbreviations, and cross-paper
   terminology. This content is data-dependent and not checked into the repo
   yet.
5. Compare candidate rerankers against `mixedbread-ai/mxbai-rerank-base-v2` on
   recall@k, nDCG@k, latency, memory use, and failure modes.
6. Validate `qwen3-embedding:4b` through the checkpointed rebuild path. Do not
   reuse a Phase C 1024d collection with 2560d runtime settings.
7. Promote a candidate only if quality improves without making PDF processing,
   Pulse, or RAG noticeably less reliable on the local hardware tier.

## Local Acceleration Notes

The one-shot `scripts/reembed.py` migration path supports `REEMBED_BACKEND=onnx`
for local bulk embedding. It now checks `onnxruntime.get_available_providers()`
before selecting `CUDAExecutionProvider`; set
`REEMBED_ONNX_REQUIRE_CUDA=true` when you want the script to fail instead of
falling back to CPU ONNX.

Re-embedding writes are intentionally ordered for retryability: Qdrant upserts
wait for completion, stale old-model points are deleted before PostgreSQL is
updated, and stale-delete failures stop the run unless
`REEMBED_CONTINUE_ON_ERROR=true` is explicitly set for diagnosis.

The runtime reranker uses the same provider discipline: ONNX/CUDA is attempted
only when `CUDAExecutionProvider` is exposed, otherwise it falls back to ONNX/CPU
and then PyTorch/CPU if ONNX export/load fails.

## Non-Goals

- No default model switch in Phase D planning docs.
- No cloud model default.
- No mixed-dimension Qdrant collection.
- No implicit Save/Open feedback training signal.

## Verified Identifiers

| Identifier | File:line | Behavior verified |
|---|---:|---|
| Current reranker default | `services/paper_ingestion/paper_ingestion/ingestion/reranker.py:35` | Runtime default is `mixedbread-ai/mxbai-rerank-base-v2`. |
| Phase C target | `docs/specs/2026-05-03-c-embedding-upgrade.md:30` | Phase C targets Qwen3-Embedding-0.6B as the production embedding model. |
| Advanced embedding catalog entry | `libs/jarvis_common/jarvis_common/data/model_catalog.json:110` | `qwen3-embedding:4b` is tracked as advanced and non-assignable. |
| Fixed eval-set loader | `scripts/eval_retrieval.py:188` | Reads JSONL scientific retrieval cases from `EVAL_RETRIEVAL_SET`. |
| Re-embed ONNX provider selection | `scripts/reembed.py:238` | Selects CUDA only when ONNX Runtime exposes it; can require CUDA via env. |
| Re-embed write ordering | `scripts/reembed.py:461` | Waits for Qdrant writes/deletes before marking PostgreSQL chunks as re-embedded. |
| Reranker ONNX provider selection | `services/paper_ingestion/paper_ingestion/ingestion/reranker.py:26` | Keeps ONNX/CUDA claims tied to `CUDAExecutionProvider`. |
