# Phase D — Scientific Retrieval Upgrade

**Status:** Planned, evaluation-only
**Date:** 2026-05-06
**Depends on:** Phase C embedding migration completion (satisfied locally on 2026-05-06)

## Purpose

Phase D tracks retrieval quality improvements for math-heavy and notation-heavy
scientific papers after the Qwen3-Embedding-0.6B migration.
It is not a blocker for Phase C and does not change runtime defaults until an
evaluation proves a better local path.

## Decisions

- Keep `qwen3-embedding:0.6b` as the Phase C production embedding model at
  1024 dimensions.
- Track `qwen3-embedding:4b` as an advanced local candidate for Tier 2+ machines,
  but keep it non-assignable until there is an explicit dimension policy:
  either a deliberate 2560d collection rebuild or a verified MRL/custom-dimension
  path that preserves the current 1024d operational contract.
- Evaluate Qwen3 rerankers against the current production reranker,
  `mixedbread-ai/mxbai-rerank-base-v2`. The older claim that production still
  used `cross-encoder/ms-marco-MiniLM-L-6-v2` is stale for this checkout.
- Prefer local models. Cloud reasoning models and cloud embedding providers stay
  future-only unless the user explicitly opts into cloud operation.

## Evaluation Plan

1. Phase C live re-embedding and PG/Qdrant parity are complete locally:
   4,888 PostgreSQL chunks and 4,888 Qdrant vectors at 1024d, all marked with
   `qwen3-embedding:0.6b`.
2. Create a fixed scientific retrieval eval set from verified papers and queries
   that include equations, symbols, abbreviations, and cross-paper terminology.
3. Run `scripts/eval_retrieval.py` against the Phase C baseline.
4. Compare candidate rerankers against `mixedbread-ai/mxbai-rerank-base-v2` on
   recall@k, nDCG@k, latency, memory use, and failure modes.
5. Evaluate `qwen3-embedding:4b` only in a separate Qdrant collection or
   checkpointed rebuild. Do not reuse the Phase C collection unless dimensions
   are deliberately matched.
6. Promote a candidate only if quality improves without making PDF processing,
   Pulse, or RAG noticeably less reliable on the local hardware tier.

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
