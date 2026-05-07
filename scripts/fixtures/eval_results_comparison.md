# Phase D Retrieval Eval — Comparison Report

**Date:** 2026-05-07 (D7 retry on Ollama 0.23.1; D6 reranker matrix)
**Status:** **SHIPPED 2026-05-07** — `qwen3-embedding:4b` is now the active production embedder.
**Corpus:** 84 papers, 4,888 chunks — port-Hamiltonian / Neural ODE / PINN domain
**Eval set:** `scripts/fixtures/retrieval_eval_set.jsonl` — 27 curated cases
**Decision threshold:** promote if nDCG@3 improves > 5% relative AND latency regression ≤ 50%

---

## Six-cell matrix

| Cell | Embedding | Reranker | P@1 | R@3 | **nDCG@3** | Mean latency |
|------|-----------|----------|-----|-----|------------|--------------|
| 1 (baseline) | 0.6b | none | 44.4% | 45.7% | **64.1%** | 205 ms |
| 2 | 0.6b | mxbai | 55.6% | 51.2% | **80.9%** | 10,507 ms |
| 3 | 0.6b | qwen3-reranker | 40.7% | 42.0% | 48.0% ❌ | 366 ms |
| 4 | 4b | none | 55.6% | 56.8% | **80.0%** | 86 ms |
| 5 | 4b | mxbai | 51.9% | 53.7% | **81.5%** | 13,605 ms |
| 6 | 4b | qwen3-reranker | 40.7% | 40.7% | 55.7% ❌ | 373 ms |

Result files: `scripts/fixtures/eval_results_baseline.json`, `eval_results_mxbai_06b.json`, `eval_results_qwen3rerank_06b.json`, `eval_results_4b_embedding.json`, `eval_results_mxbai_4b.json`, `eval_results_qwen3rerank_4b.json`.

---

## Headline decisions

### Embedding: PROMOTE `qwen3-embedding:4b`

| Gate | Threshold | Cell 4 result |
|------|-----------|---------------|
| nDCG@3 lift | > 5% relative (> 67.3% absolute) | **80.0% — +24.8% relative** ✅ |
| Latency regression | ≤ 50% (≤ 308 ms/query) | **86 ms/query — −58%** ✅ |

Both gates passed. The 4b embedding alone moves nDCG@3 from 64.1 → 80.0 % at lower mean latency than the 0.6b baseline (the 4b eval ran with the model already warm from re-embed; cold-start latency has not been measured but is bounded by the warmup probe, which finished in 91 ms after the upgrade).

### Reranker: KEEP `mxbai-rerank-base-v2`, DO NOT promote `Qwen3-Reranker-0.6B`

| Comparison | Δ nDCG@3 | Δ latency | Verdict |
|------------|----------|-----------|---------|
| Cell 5 (4b + mxbai) vs Cell 4 (4b only) | +1.9% relative | +13.5 s/query | mxbai gain is below 5% relative threshold → not worth the latency hit on the 4b stack |
| Cell 2 (0.6b + mxbai) vs Cell 1 (0.6b baseline) | +26.2% relative | +10.3 s/query | Validates the harness rerank stage; production already uses mxbai, so this is the current production stack |
| Cell 6 (4b + qwen3-rerank) vs Cell 4 (4b only) | **−30.4% relative** | +0.3 s/query | Qwen3-Reranker-0.6B is a sharp regression |
| Cell 3 (0.6b + qwen3-rerank) vs Cell 1 (baseline) | **−25.1% relative** | +0.2 s/query | Same regression on 0.6b embeddings |

`Qwen3-Reranker-0.6B` consistently degrades quality on this scientific-papers corpus, regardless of embedder. The generative `logit("yes") - logit("no")` signal does not align with paper-level relevance for our queries — a 0.6B causal LM is likely too small to score nuanced math/physics content reliably under the current zero-shot prompt template. **The adapter and harness extension remain in the codebase** so a larger Qwen3-Reranker (4B / 8B) can be evaluated later by setting `QWEN3_RERANKER_MODEL`.

`mxbai-rerank-base-v2` does what it should — adds ~1.5–17 pp absolute nDCG@3 on top of either embedder — but the 10–13 s per-query rerank cost is heavy on this hardware, and on top of the 4b embedding the gain (+1.9% relative) is below the 5% promotion threshold.

---

## Recommended production stack

**`qwen3-embedding:4b` (2560d) + `mxbai-rerank-base-v2`**

This matches what production currently runs as the *reranker*, and upgrades the embedder. The 4b-without-rerank cell is faster, but production retrieval has consumers that already depend on the rerank stage (e.g., cross-paper RAG quality on top-K wider than 3) — keeping mxbai means no behavior change beyond the embedder swap.

If latency budget tightens later, dropping the reranker is now backed by data: it costs only ~1.5 pp absolute nDCG@3 on the 4b stack and gives a ~158× per-query speedup.

### Promotion checklist (sequential)

1. Snapshot `paper_chunks` Qdrant collection (rollback safety).
2. Update `litellm/config.yaml`: replace the active `embed` alias with `ollama/qwen3-embedding:4b` (dimensions 2560). Keep `embed-4b` alias too if convenient for ad-hoc work.
3. Update `.env`: `EMBEDDING_DIMENSION=2560`, `EMBEDDING_MODEL_NAME=qwen3-embedding:4b`.
4. Run `scripts/reembed.py` against the production `paper_chunks` collection (already validated end-to-end in D7-int).
5. Restart `paper_ingestion` and `learning_engine`.
6. Smoke-test: cross-paper RAG query, paper-detail page, citation-graph query.

---

## Infrastructure change (D7-1) — record

**Ollama upgraded `0.17.7 → 0.23.1`** ([versions.env](../../versions.env#L10)). The 0.17.7 image carried a regression that prevented `qwen3-embedding:4b` from offloading to CUDA on Blackwell sm_120 GPUs ([ollama/ollama#14386](https://github.com/ollama/ollama/issues/14386)) — yesterday's D7 attempt had the model running 100 % CPU at ~25 min/embed, crashing the corpus re-embed. After the upgrade, gate test:

- `ollama ps` → `qwen3-embedding:4b … 100% GPU`
- Warm `embed-4b` via LiteLLM → **107 ms** (target: < 5 s)
- Re-embed of 84 papers / 4,888 chunks → **11 min 22 s** (target: < 1 h)

---

## Notes

- The 84-paper corpus is small enough that nDCG@3 confidence intervals are wide; require at least 27 eval cases and a > 5% relative improvement to reduce noise-driven promotion. Both met for the 4b embedding.
- D6-A (harness extension) is the durable enabler — future eval runs can swap in any reranker by setting `EVAL_RERANKER` and `EVAL_RERANK_K`.
- The Qwen3-Reranker generative-scoring adapter ([qwen3_reranker.py](../../services/paper_ingestion/paper_ingestion/ingestion/qwen3_reranker.py)) is preserved for future revisits with larger model sizes.
- All eval runs used `EVAL_RERANK_K=10` (top-10 candidates fetched then reranked to top-3) where a reranker was active; baselines used Qdrant `limit=3` directly.
