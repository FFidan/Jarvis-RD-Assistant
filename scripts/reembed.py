#!/usr/bin/env python3
"""Re-embed paper chunks with a new embedding model.

Idempotent batch script: skips papers already embedded with the target model.
Meant to be run ONCE after switching the embedding model in LiteLLM config.

Usage:
    python -m scripts.reembed
    python scripts/reembed.py

Environment variables (reads from .env or system environment):
    DATABASE_URL        - PostgreSQL connection string (or individual PG* vars)
    PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE
    QDRANT_HOST         - Qdrant hostname (default: localhost)
    QDRANT_PORT         - Qdrant port (default: 6333)
    LITELLM_BASE_URL    - LiteLLM proxy URL (default: http://localhost:4000)
    EMBEDDING_MODEL     - LiteLLM model alias (default: embed)
    EMBEDDING_MODEL_NAME- Actual model name for tracking (default: qwen3-embedding:0.6b)
    EMBEDDING_DIMENSION - Vector dimension (default: 1024)
    REEMBED_RECREATE_COLLECTION
                         - Set true to delete/recreate a wrong-dimension Qdrant collection
    REEMBED_BATCH_SIZE  - Papers per batch for progress logging (default: 5)
    REEMBED_BACKEND     - litellm, local, or onnx (default: litellm)
    REEMBED_LOCAL_MODEL - Hugging Face model for local/onnx backends
                           (default for qwen3: Qwen/Qwen3-Embedding-0.6B)
    REEMBED_ONNX_REQUIRE_CUDA
                         - Set true to fail if CUDA is available to torch but
                           onnxruntime-gpu is not installed/exposing CUDA
    REEMBED_EMBED_BATCH_SIZE
                         - Chunks per embedding batch (default: 32)
    REEMBED_BENCHMARK   - Set true to run a read-only embedding benchmark
    REEMBED_BENCHMARK_SIZE
                         - Number of chunks sampled for benchmark (default: 128)
    REEMBED_CONTINUE_ON_ERROR
                         - Set true only for debug runs that should continue after
                           per-paper failures or stale-point cleanup failures
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Protocol

import asyncpg
import httpx
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointIdsList,
    PointStruct,
    VectorParams,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


class ScriptError(RuntimeError):
    """Script-level error; caught by the __main__ block."""


logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _package_root in (
    _REPO_ROOT / "libs" / "jarvis_common",
    _REPO_ROOT / "services" / "paper_ingestion",
):
    _package_root_str = str(_package_root)
    if _package_root_str not in sys.path:
        sys.path.insert(0, _package_root_str)

if __package__:
    from scripts._db import get_dsn
else:
    from _db import get_dsn

from jarvis_common.llm_client import (  # noqa: E402
    embed_texts as embed_texts_shared,
)
from jarvis_common.llm_client import (
    get_litellm_config,
)
from paper_ingestion.ingestion.embedder import (  # noqa: E402
    extract_qdrant_collection_dimension,
    validate_embedding_configuration,
)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

LITELLM_CONFIG = get_litellm_config(base_url_default="http://localhost:4000")
LITELLM_BASE_URL = LITELLM_CONFIG.base_url
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "embed")
EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", "qwen3-embedding:0.6b")
EMBEDDING_DIMENSION = int(os.environ.get("EMBEDDING_DIMENSION", "1024"))
QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY") or None
COLLECTION_NAME = "paper_chunks"
BATCH_SIZE = int(os.environ.get("REEMBED_BATCH_SIZE", "5"))
EMBED_BATCH_SIZE = int(os.environ.get("REEMBED_EMBED_BATCH_SIZE", "32"))
REEMBED_BACKEND = os.environ.get("REEMBED_BACKEND", "litellm").strip().lower()
REEMBED_LOCAL_MODEL = os.environ.get("REEMBED_LOCAL_MODEL") or (
    "Qwen/Qwen3-Embedding-0.6B"
    if EMBEDDING_MODEL_NAME == "qwen3-embedding:0.6b"
    else EMBEDDING_MODEL_NAME
)
REEMBED_ONNX_REQUIRE_CUDA = os.environ.get("REEMBED_ONNX_REQUIRE_CUDA", "").lower() in {
    "1",
    "true",
    "yes",
}
REEMBED_BENCHMARK = os.environ.get("REEMBED_BENCHMARK", "").lower() in {"1", "true", "yes"}
REEMBED_BENCHMARK_SIZE = int(os.environ.get("REEMBED_BENCHMARK_SIZE", "128"))
REEMBED_CONTINUE_ON_ERROR = os.environ.get("REEMBED_CONTINUE_ON_ERROR", "").lower() in {
    "1",
    "true",
    "yes",
}
RECREATE_COLLECTION = os.environ.get("REEMBED_RECREATE_COLLECTION", "").lower() in {
    "1",
    "true",
    "yes",
}
REEMBED_SNAPSHOT_CONFIRMED = os.environ.get("REEMBED_SNAPSHOT_CONFIRMED", "").lower() in {
    "1",
    "true",
    "yes",
}


class EmbeddingBackend(Protocol):
    """Embedding backend contract for the one-shot re-embed script."""

    name: str

    async def embed_texts(
        self, client: httpx.AsyncClient | None, texts: list[str]
    ) -> list[list[float]]:
        """Embed texts while preserving input order."""


class LiteLLMEmbeddingBackend:
    """LiteLLM embedding backend used by the runtime service path."""

    name = "litellm"

    async def embed_texts(
        self, client: httpx.AsyncClient | None, texts: list[str]
    ) -> list[list[float]]:
        if client is None:
            raise ScriptError("LiteLLM backend requires an httpx client")
        return await embed_texts_shared(
            client,
            texts,
            model=EMBEDDING_MODEL,
            timeout=120.0,
            config=LITELLM_CONFIG,
        )


class SentenceTransformerEmbeddingBackend:
    """Local SentenceTransformers backend for bulk migration speed."""

    def __init__(self, *, use_onnx: bool = False) -> None:
        self.name = "onnx" if use_onnx else "local"
        self._use_onnx = use_onnx
        self._model: Any | None = None

    def _load_model_if_needed(self) -> Any:
        if self._model is not None:
            return self._model

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ScriptError(
                "REEMBED_BACKEND=local/onnx requires sentence-transformers. "
                "Install optional paper-ingestion dependencies first."
            ) from exc

        device = "cpu"
        try:
            import torch

            if torch.cuda.is_available():
                device = "cuda"
        except ImportError:
            torch = None  # type: ignore[assignment]

        kwargs: dict[str, Any] = {"device": device}
        if self._use_onnx:
            try:
                import onnxruntime
                import optimum  # noqa: F401
            except ImportError as exc:
                raise ScriptError(
                    "REEMBED_BACKEND=onnx requires optimum and onnxruntime. "
                    "Use REEMBED_BACKEND=local or install optional dependencies."
                ) from exc
            provider = select_onnx_provider(
                device=device,
                available_providers=list(onnxruntime.get_available_providers()),
                require_cuda=REEMBED_ONNX_REQUIRE_CUDA,
            )
            if provider == "CPUExecutionProvider":
                device = "cpu"
                kwargs["device"] = device
            kwargs.update({"backend": "onnx", "model_kwargs": {"provider": provider}})
        elif device == "cuda" and torch is not None:
            kwargs["model_kwargs"] = {"torch_dtype": torch.float16}

        logger.info(
            "Loading local embedding backend=%s model=%s device=%s",
            self.name,
            REEMBED_LOCAL_MODEL,
            device,
        )
        self._model = SentenceTransformer(REEMBED_LOCAL_MODEL, **kwargs)
        return self._model

    async def embed_texts(
        self, _client: httpx.AsyncClient | None, texts: list[str]
    ) -> list[list[float]]:
        model = self._load_model_if_needed()

        def _encode() -> list[list[float]]:
            embeddings = model.encode(
                texts,
                batch_size=EMBED_BATCH_SIZE,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            raw = embeddings.tolist() if hasattr(embeddings, "tolist") else embeddings
            return [[float(value) for value in embedding] for embedding in raw]

        return await asyncio.to_thread(_encode)


def select_onnx_provider(
    *,
    device: str,
    available_providers: list[str],
    require_cuda: bool,
) -> str:
    """Select an ONNX Runtime provider that actually exists in this environment."""
    if device == "cuda" and "CUDAExecutionProvider" in available_providers:
        return "CUDAExecutionProvider"
    if device == "cuda" and require_cuda:
        raise ScriptError(
            "REEMBED_ONNX_REQUIRE_CUDA=true but onnxruntime does not expose "
            "CUDAExecutionProvider. Install onnxruntime-gpu compatible with this CUDA stack "
            "or unset REEMBED_ONNX_REQUIRE_CUDA to allow CPU ONNX fallback."
        )
    if "CPUExecutionProvider" in available_providers:
        return "CPUExecutionProvider"
    raise ScriptError(
        "onnxruntime exposes no supported execution provider. Available providers: "
        f"{available_providers}"
    )


def build_embedding_backend(name: str | None = None) -> EmbeddingBackend:
    """Return the configured embedding backend for this run."""
    backend_name = name or REEMBED_BACKEND
    normalized = backend_name.strip().lower()
    if normalized in {"litellm", "remote"}:
        return LiteLLMEmbeddingBackend()
    if normalized in {"local", "sentence-transformers", "sentence_transformers", "st"}:
        return SentenceTransformerEmbeddingBackend(use_onnx=False)
    if normalized == "onnx":
        return SentenceTransformerEmbeddingBackend(use_onnx=True)
    raise ScriptError(
        f"Unsupported REEMBED_BACKEND={backend_name!r}. Expected one of: litellm, local, onnx."
    )


async def embed_texts(client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
    """Compatibility wrapper used by older tests."""
    return await LiteLLMEmbeddingBackend().embed_texts(client, texts)


def deterministic_point_id(paper_id: int, chunk_index: int, model_name: str) -> str:
    """Return a stable Qdrant point ID for a target paper chunk embedding."""
    digest = hashlib.sha256(f"{paper_id}:{chunk_index}:{model_name}".encode()).hexdigest()
    return f"{digest[0:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def _validate_embedding_batch(
    *,
    paper_id: int | None,
    texts: list[str],
    embeddings: list[list[float]],
) -> None:
    if len(embeddings) != len(texts):
        subject = f"paper {paper_id}" if paper_id is not None else "benchmark"
        raise ScriptError(
            f"Embedding count mismatch for {subject}: expected {len(texts)} "
            f"vectors in batch, got {len(embeddings)}"
        )
    for index, embedding in enumerate(embeddings):
        if len(embedding) != EMBEDDING_DIMENSION:
            subject = (
                f"paper {paper_id}, batch item {index}" if paper_id is not None else "benchmark"
            )
            raise ScriptError(
                f"Embedding dimension mismatch for {subject}: "
                f"got {len(embedding)}, expected {EMBEDDING_DIMENSION}"
            )


async def embed_texts_in_batches(
    backend: EmbeddingBackend,
    client: httpx.AsyncClient | None,
    texts: list[str],
    *,
    paper_id: int | None = None,
) -> list[list[float]]:
    """Embed texts in configured batches and validate count/dimension."""
    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i : i + EMBED_BATCH_SIZE]
        embeddings = await backend.embed_texts(client, batch)
        _validate_embedding_batch(paper_id=paper_id, texts=batch, embeddings=embeddings)
        all_embeddings.extend(embeddings)
    if len(all_embeddings) != len(texts):
        raise ScriptError(
            f"Embedding count mismatch for paper {paper_id}: expected {len(texts)}, "
            f"got {len(all_embeddings)}"
        )
    return all_embeddings


async def _collection_exists(qdrant: AsyncQdrantClient) -> bool:
    if hasattr(qdrant, "collection_exists"):
        return bool(await qdrant.collection_exists(collection_name=COLLECTION_NAME))
    try:
        await qdrant.get_collection(collection_name=COLLECTION_NAME)
    except Exception:
        return False
    return True


async def _create_collection(qdrant: AsyncQdrantClient) -> None:
    await qdrant.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=EMBEDDING_DIMENSION, distance=Distance.COSINE),
    )


async def ensure_collection_dimension(qdrant: AsyncQdrantClient) -> None:
    """Ensure Qdrant has the target collection with the configured vector size."""
    validate_embedding_configuration(
        model_name=EMBEDDING_MODEL_NAME,
        dimension=EMBEDDING_DIMENSION,
    )
    if not await _collection_exists(qdrant):
        logger.info(
            "Qdrant collection %s missing; creating with dimension %d",
            COLLECTION_NAME,
            EMBEDDING_DIMENSION,
        )
        await _create_collection(qdrant)
        return

    info = await qdrant.get_collection(collection_name=COLLECTION_NAME)
    current_dim = extract_qdrant_collection_dimension(info)
    if current_dim == EMBEDDING_DIMENSION:
        logger.info("Qdrant collection %s dimension is %d", COLLECTION_NAME, current_dim)
        return

    message = (
        f"Qdrant collection {COLLECTION_NAME!r} has dimension {current_dim}; "
        f"expected {EMBEDDING_DIMENSION} for {EMBEDDING_MODEL_NAME}."
    )
    if not RECREATE_COLLECTION:
        raise ScriptError(
            f"{message} Set REEMBED_RECREATE_COLLECTION=true only after taking an explicit "
            "Qdrant checkpoint/snapshot."
        )
    if not REEMBED_SNAPSHOT_CONFIRMED:
        raise ScriptError(
            f"{message} Set REEMBED_SNAPSHOT_CONFIRMED=true to confirm a Qdrant snapshot "
            "was taken before setting REEMBED_RECREATE_COLLECTION=true."
        )

    logger.warning("%s Recreating collection because REEMBED_RECREATE_COLLECTION=true.", message)
    await qdrant.delete_collection(collection_name=COLLECTION_NAME)
    await _create_collection(qdrant)


async def reembed_paper(
    paper_id: int,
    pool: asyncpg.Pool,
    qdrant: AsyncQdrantClient,
    http_client: httpx.AsyncClient,
    backend: EmbeddingBackend | None = None,
) -> int:
    """Re-embed all chunks for a single paper. Returns chunk count."""
    if backend is None:
        backend = LiteLLMEmbeddingBackend()

    # 1. Get chunks from DB
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, chunk_index, content, page_number, start_char, end_char,
                      embedding_id
               FROM paper_chunks
               WHERE paper_id = $1
               ORDER BY chunk_index""",
            paper_id,
        )

    if not rows:
        return 0

    # 2. Re-embed in batches
    texts = [r["content"] for r in rows]
    all_embeddings = await embed_texts_in_batches(backend, http_client, texts, paper_id=paper_id)

    # 3. Upsert new points to Qdrant
    points: list[PointStruct] = []
    update_rows: list[tuple[str, int]] = []
    for row, embedding in zip(rows, all_embeddings):
        point_id = deterministic_point_id(paper_id, row["chunk_index"], EMBEDDING_MODEL_NAME)
        update_rows.append((point_id, row["id"]))
        points.append(
            PointStruct(
                id=point_id,
                vector=embedding,
                payload={
                    "paper_id": paper_id,
                    "chunk_index": row["chunk_index"],
                    "page_number": row["page_number"],
                    "content": row["content"],
                    "embedding_model": EMBEDDING_MODEL_NAME,
                },
            )
        )

    # Upsert in batches of 100 to avoid oversized requests
    for i in range(0, len(points), 100):
        await qdrant.upsert(
            collection_name=COLLECTION_NAME,
            points=points[i : i + 100],
            wait=True,
        )

    # 4. Delete old Qdrant points before flipping DB rows. If cleanup fails,
    #    the DB still says the paper needs re-embedding, so a rerun remains
    #    deterministic and can retry the same stale IDs.
    new_point_ids_by_row_id = {row_id: point_id for point_id, row_id in update_rows}
    old_point_ids = [
        row["embedding_id"]
        for row in rows
        if row["embedding_id"] and row["embedding_id"] != new_point_ids_by_row_id[row["id"]]
    ]
    if old_point_ids:
        try:
            await qdrant.delete(
                collection_name=COLLECTION_NAME,
                points_selector=PointIdsList(points=old_point_ids),
                wait=True,
            )
        except Exception as exc:
            message = (
                f"Failed to delete old Qdrant points for paper {paper_id}: "
                f"{len(old_point_ids)} stale point(s) may remain"
            )
            if not REEMBED_CONTINUE_ON_ERROR:
                raise ScriptError(message) from exc
            logger.warning("%s; continuing because REEMBED_CONTINUE_ON_ERROR=true", message)

    # 5. Update DB last: set new embedding_id and embedding_model.
    async with pool.acquire() as conn:
        async with conn.transaction():
            if hasattr(conn, "executemany"):
                await conn.executemany(
                    """UPDATE paper_chunks
                       SET embedding_id = $1, embedding_model = $2
                       WHERE id = $3""",
                    [(point_id, EMBEDDING_MODEL_NAME, row_id) for point_id, row_id in update_rows],
                )
            else:
                # Test doubles may not implement executemany.
                for point_id, row_id in update_rows:
                    await conn.execute(
                        """UPDATE paper_chunks
                           SET embedding_id = $1, embedding_model = $2
                           WHERE id = $3""",
                        point_id,
                        EMBEDDING_MODEL_NAME,
                        row_id,
                    )

    return len(rows)


async def verify_postconditions(pool: asyncpg.Pool, qdrant: AsyncQdrantClient) -> None:
    """Verify DB target-model chunks and Qdrant collection vectors are in parity."""
    async with pool.acquire() as conn:
        db_target_chunks = int(
            await conn.fetchval(
                """SELECT count(*)
                   FROM paper_chunks
                   WHERE embedding_model = $1""",
                EMBEDDING_MODEL_NAME,
            )
        )
    qdrant_count_result = await qdrant.count(
        collection_name=COLLECTION_NAME,
        count_filter=Filter(
            must=[
                FieldCondition(
                    key="embedding_model",
                    match=MatchValue(value=EMBEDDING_MODEL_NAME),
                )
            ]
        ),
        exact=True,
    )
    qdrant_vectors = int(qdrant_count_result.count)
    if db_target_chunks != qdrant_vectors:
        raise ScriptError(
            "Postcondition failed: DB target-model chunk count "
            f"({db_target_chunks}) does not match Qdrant vector count for model "
            f"{EMBEDDING_MODEL_NAME!r} ({qdrant_vectors})"
        )
    logger.info(
        "Postcondition passed: DB target-model chunks=%d, Qdrant vectors for model %r=%d",
        db_target_chunks,
        EMBEDDING_MODEL_NAME,
        qdrant_vectors,
    )


async def run_benchmark(pool: asyncpg.Pool, backend: EmbeddingBackend) -> None:
    """Run a read-only embedding benchmark against sampled DB chunks."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT content
               FROM paper_chunks
               WHERE content IS NOT NULL AND content != ''
               ORDER BY id
               LIMIT $1""",
            REEMBED_BENCHMARK_SIZE,
        )
    texts = [r["content"] for r in rows]
    if not texts:
        raise ScriptError("Benchmark found no paper_chunks.content rows to embed")

    started = time.perf_counter()
    async with httpx.AsyncClient() as http_client:
        embeddings = await embed_texts_in_batches(backend, http_client, texts)
    elapsed = time.perf_counter() - started
    chunks_per_second = len(texts) / elapsed if elapsed > 0 else float("inf")
    logger.info(
        "Benchmark backend=%s model=%s chunks=%d elapsed=%.2fs chunks_per_second=%.2f dim=%d",
        backend.name,
        EMBEDDING_MODEL_NAME,
        len(texts),
        elapsed,
        chunks_per_second,
        len(embeddings[0]) if embeddings else 0,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI flags without changing env-driven defaults."""
    parser = argparse.ArgumentParser(
        description="Re-embed existing paper chunks with the configured embedding model."
    )
    parser.add_argument(
        "--backend",
        choices=["litellm", "local", "onnx"],
        help="Embedding backend for this run. Defaults to REEMBED_BACKEND or litellm.",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Run a read-only benchmark instead of writing to Qdrant/Postgres.",
    )
    parser.add_argument(
        "--benchmark-size",
        type=int,
        help="Number of chunks to embed in benchmark mode.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Debug mode: continue after per-paper failures and stale-point cleanup failures.",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    """Run the re-embedding pipeline."""
    global REEMBED_BACKEND, REEMBED_BENCHMARK, REEMBED_BENCHMARK_SIZE, REEMBED_CONTINUE_ON_ERROR

    args = parse_args([] if argv is None else argv)
    if args.backend:
        REEMBED_BACKEND = args.backend
    if args.benchmark:
        REEMBED_BENCHMARK = True
    if args.benchmark_size is not None:
        if args.benchmark_size <= 0:
            raise ScriptError("--benchmark-size must be greater than zero")
        REEMBED_BENCHMARK_SIZE = args.benchmark_size
    if args.continue_on_error:
        REEMBED_CONTINUE_ON_ERROR = True

    logger.info(
        "Starting re-embedding: target model=%s, backend=%s, LiteLLM=%s",
        EMBEDDING_MODEL_NAME,
        REEMBED_BACKEND,
        LITELLM_BASE_URL,
    )
    backend = build_embedding_backend()

    pool = await asyncpg.create_pool(get_dsn(), min_size=1, max_size=5)
    if pool is None:
        raise ScriptError("Failed to create database pool")

    try:
        if REEMBED_BENCHMARK:
            await run_benchmark(pool, backend)
            return

        qdrant = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, api_key=QDRANT_API_KEY)
        await ensure_collection_dimension(qdrant)

        # Find papers that need re-embedding
        async with pool.acquire() as conn:
            paper_ids = await conn.fetch(
                """SELECT DISTINCT paper_id
                   FROM paper_chunks
                   WHERE embedding_model != $1 OR embedding_model IS NULL
                   ORDER BY paper_id""",
                EMBEDDING_MODEL_NAME,
            )

        total = len(paper_ids)
        if total == 0:
            logger.info("All papers already embedded with %s. Nothing to do.", EMBEDDING_MODEL_NAME)
            await verify_postconditions(pool, qdrant)
            return

        logger.info("Found %d papers to re-embed", total)

        failed_paper_ids: list[int] = []
        async with httpx.AsyncClient() as http_client:
            done = 0
            total_chunks = 0
            for i in range(0, total, BATCH_SIZE):
                batch = paper_ids[i : i + BATCH_SIZE]
                for record in batch:
                    pid = record["paper_id"]
                    try:
                        count = await reembed_paper(pid, pool, qdrant, http_client, backend)
                        total_chunks += count
                        done += 1
                        logger.info("  [%d/%d] paper_id=%d  chunks=%d", done, total, pid, count)
                    except ScriptError:
                        raise
                    except Exception:
                        logger.exception("Failed to re-embed paper_id=%d", pid)
                        failed_paper_ids.append(pid)
                        done += 1

                logger.info(
                    "Batch progress: %d/%d papers done (%d chunks total)",
                    done,
                    total,
                    total_chunks,
                )

        if failed_paper_ids and not REEMBED_CONTINUE_ON_ERROR:
            failed = ", ".join(f"paper_id={pid}" for pid in failed_paper_ids)
            raise ScriptError(f"Re-embedding failed for {len(failed_paper_ids)} paper(s): {failed}")

        await verify_postconditions(pool, qdrant)
        logger.info(
            "Re-embedding complete: %d papers, %d chunks processed with model=%s",
            done,
            total_chunks,
            EMBEDDING_MODEL_NAME,
        )
    finally:
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1:]))
    except ScriptError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
