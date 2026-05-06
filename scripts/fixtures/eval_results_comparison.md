# Phase D Retrieval Eval — Comparison Report

**Date:** 2026-05-07 (D7 retry on Ollama 0.23.1)
**Corpus:** 84 papers, 4,888 chunks — port-Hamiltonian / Neural ODE / PINN domain
**Eval set:** `scripts/fixtures/retrieval_eval_set.jsonl` — 27 curated cases
**Decision threshold:** promote if nDCG@3 improves > 5% relative AND latency regression ≤ 50%

---

## Baseline (production stack)

| Metric | Value |
|--------|-------|
| Embedding | `qwen3-embedding:0.6b` (1024d, Ollama) |
| Reranker | `mixedbread-ai/mxbai-rerank-base-v2` |
| Collection | `paper_chunks` |
| P@1 | 44.4% (12/27) |
| R@3 | 45.7% (24.8/27) |
| **nDCG@3** | **64.1%** |
| Mean latency | 205 ms/query |
| Failed queries | 0 |

Full results: `scripts/fixtures/eval_results_baseline.json`

---

## D7 — qwen3-embedding:4b + paper_chunks_4b ✅ PROMOTED

**Status:** PASSED both gates. Recommended for production after a Qdrant snapshot.

**Hardware/infra change:** Ollama upgraded `0.17.7 → 0.23.1` (versions.env). The 0.17.7 release shipped with a regression that prevented qwen3-embedding GPU offload on Blackwell sm_120 GPUs ([ollama/ollama#14386](https://github.com/ollama/ollama/issues/14386)); 0.23.1 restores correct CUDA offload. Post-upgrade gate test: warm `embed-4b` returned in **107 ms**, `ollama ps` reports `100% GPU` for `qwen3-embedding:4b`.

**Re-embed (D7-int) — 2026-05-07 00:25–00:37**
- Warmup probe: **91 ms**
- Total runtime: **11 min 22 s** for 84 papers / 4,888 chunks (~430 chunks/min on RTX 5060 Ti 16GB)
- Postcondition: DB chunk count == Qdrant vector count == 4,888 ✓
- (Compare: yesterday's 0.17.7 attempt on the same model failed every paper at the 120 s timeout, ~25 min/embed CPU-bound, 0 chunks landed.)

**Eval results (paper_chunks_4b, no reranker):**

| Metric | Value | vs baseline |
|--------|-------|-------------|
| P@1 | 55.6% (15/27) | +25% relative |
| R@3 | 56.8% (15.3/27) | +24% relative |
| **nDCG@3** | **80.0%** | **+24.8% relative** |
| Mean latency | 86.6 ms/query | −58% (faster) |
| Failed queries | 0 | — |

**Notes on latency:** baseline 205 ms/query included cold-start overhead since the eval was the first thing to hit the model after stack-up. The D7 eval ran while `qwen3-embedding:4b` was already loaded on GPU from the re-embed, so per-query latency is the warm-path number. Either way the 4b stack stays comfortably under the 50% regression threshold (308 ms gate).

Full results: `scripts/fixtures/eval_results_4b_embedding.json`

---

## D6 — Reranker comparison harness ⏳ in progress

Eval harness extended ([scripts/eval_retrieval.py](../scripts/eval_retrieval.py)) with `EVAL_RERANKER ∈ {none, mxbai, qwen3-reranker}` and `EVAL_RERANK_K` controls, plus the new `Qwen3Reranker` adapter ([qwen3_reranker.py](../../services/paper_ingestion/paper_ingestion/ingestion/qwen3_reranker.py)) using a generative `logit("yes") - logit("no")` scorer. Six-cell matrix run pending: {0.6b, 4b} embeddings × {none, mxbai, qwen3-reranker} reranker.

---

## Decision Gate — Outcome

| Condition | Threshold | D7 4b result |
|-----------|-----------|--------------|
| nDCG@3 improvement | > 5% relative (> 67.3% absolute) | **PASS** (80.0%, +24.8%) |
| Latency regression | ≤ 50% (≤ 308 ms/query) | **PASS** (86.6 ms, −58%) |

**Decision:** PROMOTE `qwen3-embedding:4b` to production once the D6 reranker matrix completes (so we choose the embedding+reranker pair together).

### What "promote" means

1. Qdrant snapshot of `paper_chunks` (production safety net before any swap).
2. Change `litellm/config.yaml` `embed` alias from `ollama/qwen3-embedding:0.6b` to `ollama/qwen3-embedding:4b` (or rename the existing `embed-4b` alias to `embed`).
3. Update `EMBEDDING_DIMENSION` in production `.env` from `1024` → `2560`.
4. Update `EMBEDDING_MODEL_NAME` env var to `qwen3-embedding:4b`.
5. Run `scripts/reembed.py` against production `paper_chunks` collection.
6. Restart `paper_ingestion` and `learning_engine`.

---

## Notes

- D6 (reranker swap) is independent of D7 (embedding upgrade) — but the matrix run lets us pick the best pair on the same eval set.
- The 84-paper corpus is small enough that nDCG@3 confidence intervals are wide; require at least 27 eval cases and a > 5% relative improvement to reduce the risk of noise-driven promotion. Both met for D7.
- Baseline was run 2026-05-06 with the production `paper_chunks` collection (embedded with `qwen3-embedding:0.6b`).
- D7 ran 2026-05-07 against Ollama 0.23.1 + RTX 5060 Ti 16GB. GPU offload confirmed via `ollama ps`.
