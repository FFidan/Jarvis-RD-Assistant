# Phase D Retrieval Eval — Comparison Report

**Date:** 2026-05-06  
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

## D6 — Qwen3-Reranker-0.6B (deferred)

**Status:** DEFERRED — requires eval harness extension

The current `eval_retrieval.py` harness measures pure embedding-based retrieval quality (Qdrant approximate nearest-neighbor search). It does not include a reranking step, so swapping `mxbai-rerank-base-v2` → `Qwen3-Reranker-0.6B` cannot be evaluated by this script without modification.

**What is needed:**
1. Extend `eval_retrieval.py` to fetch top-K (e.g. 10) candidates and apply a configurable reranker before computing P@1/R@3/nDCG@3.
2. Download `Qwen/Qwen3-Reranker-0.6B` from HuggingFace — note this is a generative reranker, not a traditional cross-encoder; it may require a custom `predict()` wrapper.
3. Run eval with `RERANKER_MODEL=Qwen/Qwen3-Reranker-0.6B EVAL_RERANKER_K=10`.

**Non-blocking:** The `RERANKER_MODEL` env-var is already wired into `reranker.py` (Group 2 / D4). This work is scoped for a future eval sprint.

---

## D7 — qwen3-embedding:4b + paper_chunks_4b (infeasible on current hardware)

**Status:** INFEASIBLE on this hardware — Ollama 0.17.7 cannot GPU-offload `qwen3-embedding:4b` on the RTX 5060 Ti 16GB, falling back to 100% CPU.

**Observed runtime (2026-05-06):** Ollama embed responses on `qwen3-embedding:4b` took 24–29 minutes per call (`/api/embed` log entries: `24m17s`, `23m31s`, `25m13s`, `29m2s`). The re-embed driver had no chance: every paper hit the 2-minute httpx timeout, 0 chunks landed in `paper_chunks_4b` after 5 attempts. GPU was idle (~4 GB used of 16 GB) — Ollama explicitly chose CPU; even after unloading qwen3:14b / mistral-nemo / qwen3.5:4b the model stayed at 100% CPU.

**Latency gate verdict:** baseline single-query embedding latency is ~205 ms. CPU-bound 4b embedding clocked at ~25 minutes/query is a **~7,000× regression**, decisively failing the ≤50% regression threshold. nDCG@3 is irrelevant under this constraint.

**Conclusion:** Do NOT promote `embed-4b`. Keep the 0.6 b stack as production. The empty `paper_chunks_4b` Qdrant collection has been dropped.

**Infrastructure preserved (reusable when Ollama or hardware changes):**
- `litellm/config.yaml` — `embed-4b` alias (`ollama/qwen3-embedding:4b`, 2560d) retained
- `scripts/reembed.py` — `REEMBED_COLLECTION` env-var (Group 2 / D5)
- `REEMBED_SNAPSHOT_CONFIRMED=true REEMBED_RECREATE_COLLECTION=true` gated safely

**To retry in future** (after Ollama upgrade adds GPU offload for qwen3-embedding, or on a higher-VRAM host):

```bash
# 1. Recreate collection + re-embed:
EVAL_COLLECTION=paper_chunks_4b \
EMBEDDING_MODEL=embed-4b \
EMBEDDING_DIMENSION=2560 \
REEMBED_COLLECTION=paper_chunks_4b \
REEMBED_RECREATE_COLLECTION=true \
REEMBED_SNAPSHOT_CONFIRMED=true \
python3 scripts/reembed.py

# 2. Run eval against new collection:
EVAL_COLLECTION=paper_chunks_4b \
EMBEDDING_MODEL=embed-4b \
EMBEDDING_DIMENSION=2560 \
EVAL_OUTPUT_FILE=scripts/fixtures/eval_results_4b_embedding.json \
LITELLM_MASTER_KEY=sk-jarvis-dev-test \
python3 scripts/eval_retrieval.py
```

**Pre-flight check before retry:** verify `docker exec <ollama> ollama ps` shows `100% GPU` (not CPU) for `qwen3-embedding:4b` after a single warm-up embed, otherwise abort.

---

## Decision Gate — Outcome

| Condition | Threshold | Result |
|-----------|-----------|--------|
| D7 nDCG@3 improvement | > 5% relative (> 67.3%) | NOT EVALUABLE — model unusable on this hardware |
| D7 latency regression | ≤ 50% (≤ 308 ms/query) | **FAIL** — ~7,000× regression (CPU-bound embed ≈ 25 min/query) |
| D6 reranker swap | (separate gate) | DEFERRED — eval harness needs rerank-stage extension |

**Decision:** Keep current production stack (`qwen3-embedding:0.6b` + `mxbai-rerank-base-v2`). `paper_chunks_4b` collection dropped; `embed-4b` LiteLLM alias retained for future retries.

### What "promote" means

1. Change `litellm/config.yaml` `embed` alias from `ollama/qwen3-embedding:0.6b` to `ollama/qwen3-embedding:4b`
2. Update `EMBEDDING_DIMENSION` in production `.env` from `1024` → `2560`
3. Run `reembed.py` against production `paper_chunks` collection (requires Qdrant snapshot first)
4. Update `EMBEDDING_MODEL_NAME` env var

---

## Notes

- D6 (reranker swap) is independent of D7 (embedding upgrade) — either can be promoted separately.
- The 84-paper corpus is small enough that nDCG@3 confidence intervals are wide; require at least 27 eval cases and a > 5% relative improvement to reduce the risk of noise-driven promotion.
- Baseline was run 2026-05-06 with the production `paper_chunks` collection (embedded with `qwen3-embedding:0.6b`).
- D7 ran 2026-05-06 ~21:11 against Ollama 0.17.7 + RTX 5060 Ti 16 GB. Ollama did not offload `qwen3-embedding:4b` to GPU even with all other models unloaded; root cause likely model-architecture support gap in this Ollama version. Re-test after an Ollama upgrade or on a host with a different GPU/driver stack.
