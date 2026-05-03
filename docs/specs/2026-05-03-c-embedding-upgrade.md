# Phase C — Embedding Model Upgrade

**Status:** COMPLETE (spec ready to execute)
**Date:** 2026-05-03
**Scope:** Marathon Phase C — replace `nomic-embed-text` (768d) with Qwen3-Embedding-0.6B (1024d)
**Part of:** [docs/plans/2026-04-30-marathon-meta.md](../plans/2026-04-30-marathon-meta.md)

---

## 0. Why This Spec Exists

Phase C was reserved in the Marathon META plan as a future embedding upgrade sprint. The current model (`nomic-embed-text`, 768d) was the right call at project inception — it was already installed in Ollama and covered the initial ~600 chunks. As of 2026, stronger small embedding models have become available on Ollama with substantially higher MTEB scores and compatible licenses for self-hosted use. This spec replaces the placeholder "Future sprint" entry in the Phase Map with a concrete, executable plan.

**Nothing changes today.** Embeddings remain on `nomic-embed-text` until Phase C executes in a separate sprint. The spec is written now so the implementing agent can execute it without further design work.

---

## 1. Goals

1. Replace `nomic-embed-text` (768d) with `Qwen3-Embedding-0.6B` (1024d) as the production embedding model.
2. Re-embed all existing paper chunks via a Qdrant collection rebuild (drop + recreate + batch ingest).
3. Thread `EMBEDDING_DIMENSION` through all hardcoded `768` literals so the value is set in one place and read everywhere.
4. Remove the one remaining inline hardcode at `pulse.py:356` by importing `EMBEDDING_DIMENSION` from `ingestion.embedder`.
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

### 4.1 Current State

The embedding dimension (768) is defined and consumed in several places:

| Location | How 768 is used |
|---|---|
| `services/paper_ingestion/paper_ingestion/ingestion/embedder.py:36` | `EMBEDDING_DIMENSION = int(os.environ.get("EMBEDDING_DIMENSION", "768"))` — module-level constant, used for collection creation (`size=EMBEDDING_DIMENSION`) and per-vector validation |
| `litellm/config.yaml:66` | `dimensions: 768` — comment + inline dimension hint |
| `services/paper_ingestion/paper_ingestion/extraction/entities.py:100` | `embedding_dim = int(os.environ.get("EMBEDDING_DIMENSION", "768"))` — local re-read of same env var |
| `services/paper_ingestion/paper_ingestion/routers/pulse.py:356` | `embed_dim_expected = 768` — **inline literal, does not read `EMBEDDING_DIMENSION`** |

### 4.2 Required Change

The only new code change in Phase C is in `pulse.py`. The debug endpoint currently hardcodes 768:

```python
# Before (pulse.py:356)
embed_dim_expected = 768

# After
from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION
embed_dim_expected = EMBEDDING_DIMENSION
```

All other sites already read from the `EMBEDDING_DIMENSION` env var and will pick up the correct value when the env var is updated to 1024.

### 4.3 Environment Variable Update

```bash
# docker-compose.yml (paper_ingestion service environment)
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
- Confirm current chunk count: `SELECT COUNT(*) FROM paper_chunks;` (expected ~600).
- Record a sample of 5 queries and their top-5 RAG results (manual baseline).
- `docker exec jarvis-ollama ollama list` — confirm `nomic-embed-text` is present.

### Step 1: Pull new model into Ollama
```bash
docker exec jarvis-ollama ollama pull qwen3-embedding:0.6b
```
Verify: `docker exec jarvis-ollama ollama list | grep qwen3-embedding`

### Step 2: Update service configuration
- Edit `litellm/config.yaml`: swap embed alias from `ollama/nomic-embed-text` → `ollama/qwen3-embedding:0.6b`, update `dimensions: 1024`.
- Edit `docker-compose.yml` (paper_ingestion env): set `EMBEDDING_DIMENSION=1024`, `EMBEDDING_MODEL_NAME=qwen3-embedding:0.6b`.
- Edit `services/paper_ingestion/paper_ingestion/routers/pulse.py:356`: replace hardcoded `768` with `EMBEDDING_DIMENSION` import (§4.2).

### Step 3: Recreate Qdrant collection
The `paper_chunks` Qdrant collection was created with `size=768`. It cannot be resized in-place — it must be dropped and recreated.

```python
# One-time script (or run in a scratch container):
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance

client = QdrantClient("http://localhost:6333")

# Drop existing collection (all vectors lost — intentional)
client.delete_collection("paper_chunks")

# Recreate with new dimension
client.create_collection(
    collection_name="paper_chunks",
    vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
)
```

Alternatively: bring up the updated stack (step 5) — `Embedder.ensure_collection()` recreates the collection automatically if it does not exist. But the stale 768d collection must be deleted first; the `ensure_collection` method only creates, does not migrate.

### Step 4: Re-embed all papers (batch job)
After the collection is recreated with `size=1024`, trigger re-embedding for all papers that have `full_text` set:

```bash
# Via the paper_ingestion jobs API
curl -X POST http://localhost:8000/api/jobs \
  -H "X-API-Key: $JARVIS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"kind": "papers.batch_process", "payload": {}}'
```

The `batch_process` job calls `embedder.embed_and_store()` for each paper. Estimated throughput: ~30–60 chunks/second on RTX 5060 Ti with Ollama; ~600 chunks → under 30 seconds.

Monitor progress via `GET /api/jobs/{id}/stream` SSE.

### Step 5: Restart stack with new config
```bash
docker compose down
docker compose up -d
# After services healthy:
docker image prune -f  # clean dangling layers per Docker hygiene rules
```

LiteLLM picks up `config.yaml` on startup; paper_ingestion reads `EMBEDDING_DIMENSION` on module import.

### Step 6: Post-migration verification
1. Run the same 5 sample queries from Step 0 and compare top-5 results.
2. `GET /api/pulse/debug` — `topic_embeddings[].ok` should be `true` for all entries (previously would have been `false` if any old 768d topic embeddings persisted).
3. Confirm Qdrant collection info: `GET http://localhost:6333/collections/paper_chunks` → `vectors_count` matches PG chunk count, `config.params.vectors.size == 1024`.
4. Run `uv run pytest services/paper_ingestion/tests/` — all tests pass (embedder tests mock `EMBEDDING_DIMENSION`).

---

## 6. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| `qwen3-embedding:0.6b` tag not yet available in Ollama registry at sprint time | HIGH | Check `ollama.com/library/qwen3-embedding` before executing; fallback: use `jina-embeddings-v3` on Ollama (accept NC license for internal use) or `bge-m3` (multilingual, 1024d, MIT) |
| Qdrant collection recreate deletes all vectors permanently | HIGH | No RAG until re-embed completes (~30s); schedule during a maintenance window; script is idempotent |
| `ensure_collection()` silently skips recreate if old 768d collection still present | MEDIUM | Explicitly delete collection before restarting stack (Step 3 above); do NOT rely on `ensure_collection` alone |
| `embedding_dim = 768` in `extraction/entities.py:100` missed during update | MEDIUM | This file also reads from `EMBEDDING_DIMENSION` env var — updating the env var is sufficient; no code edit needed |
| Test fixtures hardcode 768-length vectors | LOW | Tests mock `embed_texts` and `EMBEDDING_DIMENSION`; no real dimension assertion in unit tests. Verify: `grep -r "768" services/paper_ingestion/tests/` — expected to return zero hits on embedding-dimension literals |
| Model download bandwidth (qwen3-embedding:0.6b is ~500 MB) | LOW | Pre-pull in Step 1 before any downtime begins |
| LiteLLM routing mixes old 768d and new 1024d responses during partial rollout | LOW | The embed alias has only one provider configured; latency-based routing only fires when multiple providers are listed; no mixing risk |

---

## 7. Reserved Migration

**None.** This is a Qdrant-only change. No SQL schema is affected. The `paper_chunks` table in PostgreSQL stores metadata only (no vectors). The Qdrant collection recreate is an operational step, not a database migration.

Migration number 052 is reserved by the B.4 (procrastinate) spec. The next available number for future use is 053.

---

## 8. Test Fixture Impact

Embedding unit tests in `services/paper_ingestion/tests/` mock `embed_texts` at the `Embedder` level and pass fake fixed-length vectors. They do not assert on the specific dimension 768. The `EMBEDDING_DIMENSION` constant is imported from `ingestion.embedder` at module import time — tests that mock `embed_texts` do not exercise the collection creation path.

**Action required before Phase C sprint:** run `grep -rn "768" services/paper_ingestion/tests/` to confirm no test hardcodes that integer as an expected vector dimension. If any are found, update them to read `EMBEDDING_DIMENSION` from the environment.

---

## 9. Open Questions (for implementation sprint)

1. **Qwen3 context window:** Qwen3-Embedding-0.6B supports 32k token context — confirm that the 512-token chunk limit in `CHUNK_TOKEN_LIMIT` is still appropriate or take advantage of longer chunks (would improve recall on long methodology sections).
2. **MRL support:** Qwen3-Embedding supports Matryoshka Representation Learning — if Qdrant collection size ever needs to shrink, project to 512d or 256d without re-embedding. Document this option.
3. **Retrieval benchmark:** a 5-query manual baseline (Step 0) is lightweight. Consider adding a lightweight labeled eval script (`scripts/eval_rag.py`, analogous to `scripts/eval_pulse.py`) with precision@5 to make regression detectable.
4. **nomic-embed-text removal:** after successful re-embedding, `docker exec jarvis-ollama ollama rm nomic-embed-text` frees ~300 MB VRAM headroom. Confirm no other workflow still uses it directly (check for hardcoded `nomic-embed-text` strings beyond the LiteLLM config and `EMBEDDING_MODEL_NAME` env).

---

## Verified Identifiers

Every cited identifier was Read in this session via the Read or Bash tool against HEAD (`master` @ `69eac2f`).

| Citation | File:line | Behavior |
|---|---|---|
| `embed_dim_expected = 768` (inline literal) | [services/paper_ingestion/paper_ingestion/routers/pulse.py:356](../../services/paper_ingestion/paper_ingestion/routers/pulse.py#L356) | Local variable used to validate topic embedding dimension in `GET /api/pulse/debug`; does not read `EMBEDDING_DIMENSION` env var — must be changed to `from paper_ingestion.ingestion.embedder import EMBEDDING_DIMENSION` |
| `EMBEDDING_DIMENSION` | [services/paper_ingestion/paper_ingestion/ingestion/embedder.py:36](../../services/paper_ingestion/paper_ingestion/ingestion/embedder.py#L36) | `int(os.environ.get("EMBEDDING_DIMENSION", "768"))` — module-level constant read from env; drives Qdrant collection `size=` and per-vector dimension validation |
| `EMBEDDING_MODEL` | [services/paper_ingestion/paper_ingestion/ingestion/embedder.py:34](../../services/paper_ingestion/paper_ingestion/ingestion/embedder.py#L34) | `os.environ.get("EMBEDDING_MODEL", "embed")` — LiteLLM alias used in `/v1/embeddings` requests; stays `"embed"` after Phase C |
| `EMBEDDING_MODEL_NAME` | [services/paper_ingestion/paper_ingestion/ingestion/embedder.py:35](../../services/paper_ingestion/paper_ingestion/ingestion/embedder.py#L35) | `os.environ.get("EMBEDDING_MODEL_NAME", "nomic-embed-text")` — human-readable name stored in chunk metadata; must be updated to `qwen3-embedding:0.6b` |
| `COLLECTION_NAME` | [services/paper_ingestion/paper_ingestion/ingestion/embedder.py:39](../../services/paper_ingestion/paper_ingestion/ingestion/embedder.py#L39) | `"paper_chunks"` — Qdrant collection name; unchanged by Phase C |
| `ensure_collection` | [services/paper_ingestion/paper_ingestion/ingestion/embedder.py:72-96](../../services/paper_ingestion/paper_ingestion/ingestion/embedder.py#L72-L96) | Creates Qdrant collection with `size=EMBEDDING_DIMENSION` if absent; idempotent; does NOT recreate/migrate existing collections |
| `embedding_dim = 768` (entities) | [services/paper_ingestion/paper_ingestion/extraction/entities.py:100](../../services/paper_ingestion/paper_ingestion/extraction/entities.py#L100) | `int(os.environ.get("EMBEDDING_DIMENSION", "768"))` — reads env var; no code change needed, only env var update |
| LiteLLM `embed` alias | [litellm/config.yaml:62-66](../../litellm/config.yaml#L62-L66) | `model: "ollama/nomic-embed-text"`, `dimensions: 768` — must be updated to `qwen3-embedding:0.6b` / 1024 |
| `llm.embed_model` settings key | [services/paper_ingestion/paper_ingestion/routers/settings.py:53](../../services/paper_ingestion/paper_ingestion/routers/settings.py#L53) | Settings UI field for the embed model alias; routes to `litellm_config.update_litellm_model`; no change required in Phase C (alias stays `"embed"`) |
