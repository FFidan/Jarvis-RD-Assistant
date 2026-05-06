# Phase C — Embedding Model Upgrade

**Status:** SHIPPED — live re-embed complete on the local stack
**Date:** 2026-05-03
**Scope:** Marathon Phase C — replace `nomic-embed-text` (768d) with Qwen3-Embedding-0.6B (1024d)
**Part of:** [docs/plans/2026-04-30-marathon-meta.md](../plans/2026-04-30-marathon-meta.md)

---

## 0. Why This Spec Exists

Phase C was reserved in the Marathon META plan as a future embedding upgrade sprint. The current model (`nomic-embed-text`, 768d) was the right call at project inception — it was already installed in Ollama and covered the initial ~600 chunks. As of 2026, stronger small embedding models have become available on Ollama with substantially higher MTEB scores and compatible licenses for self-hosted use. This spec replaces the placeholder "Future sprint" entry in the Phase Map with a concrete, executable plan.

**Implementation update (2026-05-06):** code and default config target
`qwen3-embedding:0.6b` at 1024 dimensions, and the local live migration is
complete. Ollama has `qwen3-embedding:0.6b`; Qdrant `paper_chunks` is 1024d
with 4,888 points; PostgreSQL has 4,888 chunks total, all stored with
`embedding_model = 'qwen3-embedding:0.6b'`; and the destructive checkpoint
remains available as
`paper_chunks-6957443329211142-2026-05-05-22-27-15.snapshot`. The canonical
path remains `scripts/reembed.py`, which fails closed on per-paper failures,
stale-point cleanup failures, and final PG/Qdrant parity mismatches unless
`REEMBED_CONTINUE_ON_ERROR=true` is set for a debug run.

---

## 1. Goals

1. Replace `nomic-embed-text` (768d) with `Qwen3-Embedding-0.6B` (1024d) as the production embedding model.
2. Re-embed all existing paper chunks via a Qdrant collection rebuild (drop + recreate + batch ingest).
3. Thread `EMBEDDING_DIMENSION` through hardcoded `768` literals so the value is set in one place and read everywhere.
4. Remove the Pulse debug inline hardcode by importing `EMBEDDING_DIMENSION` from `ingestion.embedder`.
5. Validate retrieval quality on a sample query set before and after.

---

## 2. Non-Goals

- Multi-model embedding (no per-paper-type routing).
- Cloud embedding providers (OpenAI `text-embedding-3-small` stays commented out in `litellm/config.yaml`).
- BERTopic or topic-model integration (deferred to Phase D).
- Changing the Qdrant collection name or adding metadata namespaces.
- Any SQL schema migration (Qdrant is the only vector store; no PG impact).

---

## 3. Decision Matrix

| Model | Dim | MTEB avg (BEIR/STS) | License | Ollama tag | Notes |
|---|---|---|---|---|---|
| **Qwen3-Embedding-0.6B** ← **recommended** | 1024 | ~65–67 (Apr 2026 leaderboard) | Apache 2.0 | `qwen3-embedding:0.6b` | Strongest sub-1B model as of May 2026; Alibaba Qwen3 family; truncation-safe; Ollama tag shipping; MRL-compatible (can project to lower dims if needed) |
| Jina-Embeddings-v5 (jina-embeddings-v3) | 1024 | ~62–64 | CC BY-NC 4.0 | `jina-embeddings-v3` | Non-commercial license — **disqualifying** for any future commercial use; strong on long-document retrieval; better than nomic but weaker than Qwen3 |
| BGE-small-en-v1.5 | 384 | ~51–53 | MIT | `bge-small-en-v1.5` (via Ollama) | Smallest option; well-tested; MIT; significantly weaker than both above on MTEB; 384d means lower representation capacity for scientific text |
| nomic-embed-text (current) | 768 | ~54–56 | Apache 2.0 | `nomic-embed-text` | Baseline; outperformed by Qwen3 on science/BEIR benchmarks; no active improvement upstream |

### Recommendation: Qwen3-Embedding-0.6B

**Rationale:**
- Highest MTEB score of the sub-1B candidates as of May 2026.
- Apache 2.0 license — no commercial restrictions.
- Ships on Ollama under `qwen3-embedding:0.6b`; same `api_base` as current setup.
- 1024d vectors require a Qdrant collection recreate (not additive) but the rebuild is already required for any model swap — no extra cost.
- Jina-v3 is eliminated by its CC BY-NC license. BGE-small is too weak for scientific-paper RAG (low recall on specialized terminology). nomic is the baseline we're replacing.

---

## 4. Dimension Threading

### 4.1 Current Implementation

As of 2026-05-05:

| Location | Current value |
|---|---|
| `services/paper_ingestion/paper_ingestion/ingestion/embedder.py` | Defaults to `EMBEDDING_MODEL_NAME=qwen3-embedding:0.6b` and `EMBEDDING_DIMENSION=1024`. |
| `litellm/config.yaml` | `embed` alias points at `ollama/qwen3-embedding:0.6b` with `dimensions: 1024`. |
| `docker-compose.yml` / `.env.example` | Pass `EMBEDDING_MODEL_NAME=qwen3-embedding:0.6b` and `EMBEDDING_DIMENSION=1024`; bootstrap model list includes `qwen3-embedding:0.6b`. |
| `services/paper_ingestion/paper_ingestion/routers/pulse.py` | `GET /api/pulse/debug` validates topic embeddings against `EMBEDDING_DIMENSION`, not a literal. |
| `scripts/reembed.py` | Canonical re-embedding path; refuses wrong-dimension Qdrant collections unless `REEMBED_RECREATE_COLLECTION=true` is explicitly set, supports deterministic point IDs, and can benchmark LiteLLM/local/ONNX backends without writes. |

### 4.2 Historical State

The embedding dimension (768) is defined and consumed in several places:

| Location | How 768 is used |
|---|---|
| `services/paper_ingestion/paper_ingestion/ingestion/embedder.py:36` | `EMBEDDING_DIMENSION = int(os.environ.get("EMBEDDING_DIMENSION", "768"))` — module-level constant, used for collection creation (`size=EMBEDDING_DIMENSION`) and per-vector validation |
| `litellm/config.yaml:66` | `dimensions: 768` — comment + inline dimension hint |
| `services/paper_ingestion/paper_ingestion/extraction/entities.py:100` | `embedding_dim = int(os.environ.get("EMBEDDING_DIMENSION", "768"))` — local re-read of same env var |
| `services/paper_ingestion/paper_ingestion/routers/pulse.py:356` | `embed_dim_expected = 768` — **inline literal, does not read `EMBEDDING_DIMENSION`** |

### 4.3 Implemented Code Change

The Pulse debug endpoint previously hardcoded 768:

```python
# Before (pulse.py:356)
embed_dim_expected = 768

# After
from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION
embed_dim_expected = EMBEDDING_DIMENSION
```

The embedding defaults in the embedder and entity extraction paths now also
default to 1024, so a missing env var no longer silently restores the legacy
768-dimensional contract.

### 4.4 Environment Variable Update

```bash
EMBEDDING_DIMENSION=1024
EMBEDDING_MODEL_NAME=qwen3-embedding:0.6b
# EMBEDDING_MODEL stays "embed" — the LiteLLM alias, not the model name directly
```

### 4.4 LiteLLM Config Update

```yaml
# litellm/config.yaml — embed model block
- model_name: "embed"
  litellm_params:
    model: "ollama/qwen3-embedding:0.6b"
    api_base: "http://ollama:11434"
    dimensions: 1024  # qwen3-embedding:0.6b outputs 1024d
```

---

## 5. Migration Plan

### Step 0: Pre-flight checks
- Confirm current PG chunk state: `SELECT COUNT(*), embedding_model FROM paper_chunks GROUP BY embedding_model;` (2026-05-06 live fact: 4,888 Qwen3 chunks, 0 Nomic chunks).
- Record current Qdrant collection size/dimension and vector count (2026-05-06 live fact: `paper_chunks` is 1024d with 4,888 points).
- Confirm `GET /collections/paper_chunks/snapshots` still lists the 2026-05-05T22:27:16 snapshot before any future destructive operation.
- Run `python -m scripts.eval_retrieval` if the local stack has verified eval
  fixtures/results; otherwise record a manual sample of 5 queries and top-5 RAG
  results as the baseline.
- Confirm the legacy model and collection state before changing the stack.
- Confirm `qwen3-embedding:0.6b` is available to the local Ollama runtime
  (pre-migration live fact: installed).

### Step 1: Verify model in Ollama
```bash
docker exec jarvis-ollama ollama list | grep qwen3-embedding
```
If the tag is absent on a different host, pull it before continuing:
`docker exec jarvis-ollama ollama pull qwen3-embedding:0.6b`.

### Step 2: Verify service configuration
- `litellm/config.yaml` should route the `embed` alias to `ollama/qwen3-embedding:0.6b` with `dimensions: 1024`.
- `docker-compose.yml` and `.env.example` should pass `EMBEDDING_MODEL_NAME=qwen3-embedding:0.6b` and `EMBEDDING_DIMENSION=1024`.
- Existing local `.env` files are not version-controlled. Update any old `.env` that still contains `EMBEDDING_DIMENSION=768`; the service now reports a degraded `embedding_config` issue and startup collection checks fail clearly when Qwen3 is paired with 768d config.

### Step 3: Take or verify the destructive Qdrant checkpoint
The original `paper_chunks` Qdrant collection was created with `size=768`. It
cannot be resized in-place. The local stack has already been recreated to 1024d,
with one snapshot recorded on 2026-05-05. On a different host, record collection
metadata and take an operator checkpoint/snapshot before deleting any collection.

Do not set the recreate flag until the checkpoint exists. `scripts/reembed.py`
will refuse a wrong-dimension collection by default and only delete/recreate it
when `REEMBED_RECREATE_COLLECTION=true` is explicit. If the collection is already
1024d, leave the recreate flag unset and resume the remaining chunks.

The `kg_entities` Qdrant collection is optional KG dedup metadata. If it still
has the old dimension, semantic entity dedup degrades to exact-name matching
until you take a separate snapshot/rebuild decision; the application does not
delete or recreate `kg_entities` automatically.

### Step 4: Restart stack with new config

```bash
docker compose down
docker compose up -d
```

LiteLLM picks up `config.yaml` on startup; paper_ingestion reads
`EMBEDDING_DIMENSION` on module import. Startup validates known
model/dimension pairs and the existing `paper_chunks` Qdrant collection
dimension, so stale local env or an unrecreated 768d collection fail with a
direct diagnostic instead of surfacing later as embedding errors.

### Step 5: Re-embed all existing chunks

Use `scripts/reembed.py`; it is the canonical migration path for already
processed chunks.

```bash
REEMBED_RECREATE_COLLECTION=true python -m scripts.reembed
```

For a partially migrated 1024d collection on another host, leave
`REEMBED_RECREATE_COLLECTION` unset and choose the fastest verified backend:

```bash
REEMBED_BENCHMARK=true REEMBED_BENCHMARK_SIZE=128 REEMBED_BACKEND=litellm python -m scripts.reembed
REEMBED_BENCHMARK=true REEMBED_BENCHMARK_SIZE=128 REEMBED_BACKEND=local python -m scripts.reembed
REEMBED_BENCHMARK=true REEMBED_BENCHMARK_SIZE=128 REEMBED_BACKEND=onnx python -m scripts.reembed

REEMBED_BACKEND=local python -m scripts.reembed
```

2026-05-06 local benchmark result: `REEMBED_BACKEND=local` processed 128
chunks in 60.98s (2.10 chunks/s) with 1024d output and was used to complete the
live backfill. ONNX was not used on this host because `onnxruntime` did not
expose `CUDAExecutionProvider` (`AzureExecutionProvider` and
`CPUExecutionProvider` only). LiteLLM embedding calls are correctly authenticated
through `LITELLM_MASTER_KEY`; unauthenticated benchmark attempts failed with 401
and authenticated LiteLLM throughput was slower than the local backend.

Do not use the `papers.batch_process` job for this migration. The live
`papers.batch_process` path calls `run_process_pdf()` without `force=True`; for
papers whose chunks are already processed, that path can skip re-embedding and
leave old vectors absent or stale after collection recreation.

### Step 6: Post-migration verification
1. Run the same 5 sample queries from Step 0 and compare top-5 results.
   2026-05-06 live fact: `python -m scripts.eval_retrieval` runs cleanly, but
   the local stack has no verified findings yet, so it exits with "No verified
   findings found. Cannot evaluate retrieval."
2. `GET /api/pulse/debug` — `topic_embeddings[].ok` should be `true` for all
   entries (previously would have been `false` if any old 768d topic embeddings
   persisted). 2026-05-06 live fact: an on-demand Pulse job generated a new
   deck and the latest debug response reports `degraded_reason: null`.
3. Confirm Qdrant collection info: `GET http://localhost:6333/collections/paper_chunks` → vector count matches PG chunk count and `config.params.vectors.size == 1024`. 2026-05-06 live fact: 4,888 vectors at 1024d.
4. Confirm all DB chunks carry `embedding_model = 'qwen3-embedding:0.6b'`.
   2026-05-06 live fact: 4,888/4,888 chunks carry the Qwen3 model marker.
5. Run `uv run pytest services/paper_ingestion/tests/test_reembed.py` and the targeted embedding/Pulse tests.

---

## 6. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| `qwen3-embedding:0.6b` tag not yet available in Ollama registry at sprint time | HIGH | Check `ollama.com/library/qwen3-embedding` before executing; fallback: use `jina-embeddings-v3` on Ollama (accept NC license for internal use) or `bge-m3` (multilingual, 1024d, MIT) |
| Qdrant collection recreate deletes all vectors permanently | HIGH | `scripts/reembed.py` refuses wrong-dimension deletion unless `REEMBED_RECREATE_COLLECTION=true` is explicit after an operator checkpoint |
| `papers.batch_process` skips already-processed chunks | HIGH | Use `scripts/reembed.py`; do not rely on `papers.batch_process` for this migration |
| `ensure_collection()` silently skips recreate if old 768d collection still present | MEDIUM | Startup now checks existing `paper_chunks` dimensions and fails clearly; `scripts/reembed.py` refuses/recreates according to the explicit flag |
| `kg_entities` remains at old dimension | MEDIUM | KG semantic dedup degrades without automatic deletion; rebuild separately after an explicit checkpoint if needed |
| `embedding_dim = 768` default in `extraction/entities.py` missed during update | MEDIUM | Code default now uses 1024, matching `EMBEDDING_DIMENSION`, and checks existing collection dimensions |
| Test fixtures hardcode 768-length vectors | LOW | Tests now import `EMBEDDING_DIMENSION` or use 1024; verify with `rg "768" services/paper_ingestion/tests/` |
| Model download bandwidth (qwen3-embedding:0.6b is ~500 MB) | LOW | Pre-pull in Step 1 before any downtime begins |
| LiteLLM routing mixes old 768d and new 1024d responses during partial rollout | LOW | The embed alias has only one provider configured; latency-based routing only fires when multiple providers are listed; no mixing risk |

---

## 7. Reserved Migration

**None.** This is a Qdrant-only change. No SQL schema is affected. The `paper_chunks` table in PostgreSQL stores metadata only (no vectors). The Qdrant collection recreate is an operational step, not a database migration.

Migration number 052 is reserved by the B.4 (procrastinate) spec. The next
available number for future SQL migrations is 058.

---

## 8. Test Fixture Impact

Embedding unit tests in `services/paper_ingestion/tests/` mock `embed_texts` at
the `Embedder` or re-embed backend level and pass fake fixed-length vectors.
Phase C tests now cover the Qdrant collection-dimension guard, explicit recreate
flag, deterministic point IDs, backend selection, read-only benchmark mode,
embedding count mismatch, and embedding dimension mismatch.

---

## 9. Open Questions (for implementation sprint)

1. **Qwen3 context window:** Qwen3-Embedding-0.6B supports 32k token context — confirm that the 512-token chunk limit in `CHUNK_TOKEN_LIMIT` is still appropriate or take advantage of longer chunks (would improve recall on long methodology sections).
2. **MRL support:** Qwen3-Embedding supports Matryoshka Representation Learning — if Qdrant collection size ever needs to shrink, project to 512d or 256d without re-embedding. Document this option.
3. **Retrieval benchmark:** a 5-query manual baseline (Step 0) is lightweight. Consider adding a lightweight labeled eval script (`scripts/eval_rag.py`, analogous to `scripts/eval_pulse.py`) with precision@5 to make regression detectable.
4. **nomic-embed-text removal:** after successful re-embedding, `docker exec jarvis-ollama ollama rm nomic-embed-text` frees ~300 MB VRAM headroom. Confirm no archived or compatibility workflow still uses it directly.

---

## Verified Identifiers

Every cited identifier below was re-read during the 2026-05-05 implementation pass.

| Citation | File:line | Behavior |
|---|---|---|
| `embed_dim_expected` | [services/paper_ingestion/paper_ingestion/routers/pulse.py](../../services/paper_ingestion/paper_ingestion/routers/pulse.py) | `GET /api/pulse/debug` validates topic embedding length against imported `EMBEDDING_DIMENSION`. |
| `EMBEDDING_DIMENSION` | [services/paper_ingestion/paper_ingestion/ingestion/embedder.py](../../services/paper_ingestion/paper_ingestion/ingestion/embedder.py) | Module-level constant defaults to 1024 and drives Qdrant collection creation plus per-vector validation. |
| `EMBEDDING_MODEL` | [services/paper_ingestion/paper_ingestion/ingestion/embedder.py:34](../../services/paper_ingestion/paper_ingestion/ingestion/embedder.py#L34) | `os.environ.get("EMBEDDING_MODEL", "embed")` — LiteLLM alias used in `/v1/embeddings` requests; stays `"embed"` after Phase C |
| `EMBEDDING_MODEL_NAME` | [services/paper_ingestion/paper_ingestion/ingestion/embedder.py](../../services/paper_ingestion/paper_ingestion/ingestion/embedder.py) | Human-readable model name defaults to `qwen3-embedding:0.6b` and is stored in chunk metadata. |
| `COLLECTION_NAME` | [services/paper_ingestion/paper_ingestion/ingestion/embedder.py:39](../../services/paper_ingestion/paper_ingestion/ingestion/embedder.py#L39) | `"paper_chunks"` — Qdrant collection name; unchanged by Phase C |
| `ensure_collection` | [services/paper_ingestion/paper_ingestion/ingestion/embedder.py](../../services/paper_ingestion/paper_ingestion/ingestion/embedder.py) | Creates Qdrant collection with `size=EMBEDDING_DIMENSION` if absent; validates existing collection dimensions; does NOT recreate/migrate existing collections |
| `embedding_dim` (entities) | [services/paper_ingestion/paper_ingestion/extraction/entities.py](../../services/paper_ingestion/paper_ingestion/extraction/entities.py) | Entity extraction embedding dimension fallback now defaults to 1024. |
| LiteLLM `embed` alias | [litellm/config.yaml](../../litellm/config.yaml) | Routes `embed` to `ollama/qwen3-embedding:0.6b` with `dimensions: 1024`. |
| `llm.embed_model` settings key | [services/paper_ingestion/paper_ingestion/routers/settings.py:53](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L53) | Settings UI field for the embed model alias; routes to `litellm_config.update_litellm_model`; no change required in Phase C (alias stays `"embed"`) |
| `scripts.reembed.ensure_collection_dimension` | [scripts/reembed.py](../../scripts/reembed.py) | Checks Qdrant collection dimension and refuses wrong-dimension recreation unless `REEMBED_RECREATE_COLLECTION=true` is explicit. |
| `scripts.reembed.build_embedding_backend` | [scripts/reembed.py](../../scripts/reembed.py) | Selects the LiteLLM, local SentenceTransformers, or ONNX backend for bulk re-embedding. |
| `scripts.reembed.run_benchmark` | [scripts/reembed.py](../../scripts/reembed.py) | Samples paper chunks and embeds them without writing to Qdrant or PostgreSQL. |
