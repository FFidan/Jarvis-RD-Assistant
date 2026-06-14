"""Typed pydantic-settings configuration for the Paper Ingestion service.

Bucket H — migrates paper-ingestion-specific ``os.getenv`` call sites
to a typed ``PaperIngestionSettings`` class. Inherits shared infra keys from
``JarvisCommonSettings``.

1:1 env-var table (paper-ingestion layer)
------------------------------------------
Env var                     Field                       Call sites
---                         ---                         ---
QDRANT_URL                  qdrant_url                  main.py, embedder.py
QDRANT_API_KEY              qdrant_api_key              main.py
OLLAMA_BASE_URL             ollama_base_url             routers/settings.py, routers/setup.py,
                                                         routers/system.py (×3)
EMBEDDING_MODEL             embedding_model             ingestion/embedder.py
EMBEDDING_MODEL_NAME        embedding_model_name        ingestion/embedder.py
EMBEDDING_DIMENSION         embedding_dimension         ingestion/embedder.py,
                                                         extraction/entities.py
EMBED_REQUEST_TIMEOUT_SECONDS embed_request_timeout_seconds ingestion/embedder.py
RERANKER_MODEL              reranker_model              ingestion/reranker.py
QWEN3_RERANKER_MODEL        qwen3_reranker_model        ingestion/qwen3_reranker.py
PDF_STORAGE_PATH            pdf_storage_path            pdf_processor.py, routers/analyze.py
SNAPSHOT_STORAGE_PATH       snapshot_storage_path       pdf_processor.py, card_generator.py,
                                                         services/summarization.py
LOCAL_PDF_SCAN_DIR          local_pdf_scan_dir          services/local_pdfs.py
BBT_BASE_URL                bbt_base_url                integrations/zotero_client.py
APP_BASE_URL                app_base_url                routers/auth.py, routers/admin.py
AUTO_FETCH_INTERVAL_HOURS   auto_fetch_interval_hours   main.py, pipelines/auto_fetch.py
PULSE_STAGE2_MODEL          pulse_stage2_model          pulse/scoring.py
PULSE_STAGE2_MAX_RETRIES    pulse_stage2_max_retries    pulse/scoring.py
PULSE_STAGE2_TIMEOUT_SECONDS pulse_stage2_timeout_seconds pulse/job.py
PULSE_LLM_CONCURRENCY       pulse_llm_concurrency       pulse/scoring.py
SEMANTIC_SCHOLAR_API_KEY    semantic_scholar_api_key    routers/search_helpers.py,
                                                         sources/semantic_scholar_source.py
PUBMED_API_KEY              pubmed_api_key              sources/pubmed_source.py
OPENALEX_API_KEY            openalex_api_key            sources/openalex_source.py
OPENALEX_EMAIL              openalex_email              sources/openalex_source.py
INFRA_INGEST_KEY            infra_ingest_key            routers/infra_events.py
INFRA_INGEST_KEY_FILE       infra_ingest_key_file       routers/infra_events.py
TELEGRAM_BOT_TOKEN          telegram_bot_token          routers/system.py
VECTOR_API_URL              vector_api_url              main.py health check
"""

from __future__ import annotations

from jarvis_common.config import JarvisCommonSettings
from pydantic import Field, SecretStr

__all__ = [
    "ALLOWED_PDF_DOMAINS",
    "PaperIngestionSettings",
    "get_paper_ingestion_settings",
]

# ---------------------------------------------------------------------------
# Module-level constants (no heavy imports required)
# ---------------------------------------------------------------------------

#: Domains from which PDF downloads are permitted.  Source plugins import
#: this directly so they never transitively pull the heavy pdf_processor module.
ALLOWED_PDF_DOMAINS: frozenset[str] = frozenset(
    {
        "arxiv.org",
        "export.arxiv.org",
        "www.arxiv.org",
        "pdfs.semanticscholar.org",
        "www.semanticscholar.org",
    }
)


class PaperIngestionSettings(JarvisCommonSettings):
    """Typed settings for Paper Ingestion service env vars.

    Extends ``JarvisCommonSettings`` with PI-specific keys.  All fields map
    1:1 to the existing env vars — no drops, no renames.
    """

    # --- Qdrant vector store --------------------------------------------
    qdrant_url: str = Field(
        default="http://qdrant:6333",
        description="Qdrant server URL (QDRANT_URL).",
    )
    qdrant_api_key: SecretStr | None = Field(
        default=None,
        description="Optional Qdrant API key (QDRANT_API_KEY).",
    )

    # --- Ollama ---------------------------------------------------------
    ollama_base_url: str = Field(
        default="http://ollama:11434",
        description="Ollama server base URL (OLLAMA_BASE_URL).",
    )
    vector_api_url: str = Field(
        default="http://vector:8686",
        description="Vector sidecar API URL for best-effort health checks (VECTOR_API_URL).",
    )

    # --- Embedding ------------------------------------------------------
    embedding_model: str = Field(
        default="embed",
        description=(
            "LiteLLM alias for the embedding model (EMBEDDING_MODEL). "
            "Resolved via LiteLLM; default 'embed' maps to whatever is "
            "configured in litellm.yaml."
        ),
    )
    embedding_model_name: str = Field(
        default="qwen3-embedding:4b",
        description="Ollama model name for direct embedding calls (EMBEDDING_MODEL_NAME).",
    )
    embedding_dimension: int = Field(
        default=2560,
        description="Dimensionality of the embedding vectors (EMBEDDING_DIMENSION).",
    )
    embed_request_timeout_seconds: float = Field(
        default=300.0,
        description=(
            "Read timeout in seconds for a single LiteLLM /v1/embeddings call "
            "(EMBED_REQUEST_TIMEOUT_SECONDS).  Must tolerate the embedding model "
            "being GPU-evicted to CPU on memory-constrained machines, where a "
            "batch of 32 chunks can take minutes.  Default 300 s matches the "
            "service HTTP client read timeout; lower it only on fast dedicated "
            "embedding hardware."
        ),
    )

    # --- Reranker -------------------------------------------------------
    reranker_model: str = Field(
        default="mixedbread-ai/mxbai-rerank-base-v2",
        description="HuggingFace model ID for the cross-encoder reranker (RERANKER_MODEL).",
    )
    qwen3_reranker_model: str = Field(
        default="Qwen/Qwen3-Reranker-0.6B",
        description="HuggingFace model ID for the Qwen3 reranker (QWEN3_RERANKER_MODEL).",
    )

    # --- Storage paths --------------------------------------------------
    pdf_storage_path: str = Field(
        default="/data/pdfs",
        description="Directory where downloaded PDF files are stored (PDF_STORAGE_PATH).",
    )
    snapshot_storage_path: str = Field(
        default="/data/snapshots",
        description=(
            "Directory for paper analysis snapshot files (SNAPSHOT_STORAGE_PATH). "
            "Shared by pdf_processor, summarization, and card_generator."
        ),
    )
    local_pdf_scan_dir: str = Field(
        default="/data/local_pdfs",
        description=(
            "Directory scanned by the local-PDF source for files to ingest (LOCAL_PDF_SCAN_DIR)."
        ),
    )

    # --- Docling extraction ---------------------------------------------
    docling_artifacts_path: str | None = Field(
        default=None,
        description=(
            "Local path to prefetched Docling model artifacts "
            "(DOCLING_ARTIFACTS_PATH).  None = Docling downloads + caches models "
            "on first use; set this in offline/air-gapped deployments."
        ),
    )

    # --- Zotero / BBT ---------------------------------------------------
    bbt_base_url: str = Field(
        default="http://host.docker.internal:23119",
        description=(
            "Better BibTeX (BBT) server base URL (BBT_BASE_URL).  "
            "Validated at startup via validate_bbt_base_url() to block SSRF."
        ),
    )

    # --- App base URL ---------------------------------------------------
    app_base_url: str | None = Field(
        default=None,
        description=(
            "Public-facing base URL for this app (APP_BASE_URL).  Used in "
            "magic-link emails (auth router) and admin invite links.  "
            "None = Caddy/nginx resolves it from the request Host header."
        ),
    )

    # --- Auto-fetch scheduler -------------------------------------------
    auto_fetch_interval_hours: float = Field(
        default=0.0,
        description=(
            "Hours between automatic paper fetches (AUTO_FETCH_INTERVAL_HOURS). "
            "0 = scheduler starts but no periodic job is registered."
        ),
    )

    # --- Pulse ----------------------------------------------------------
    pulse_stage2_model: str = Field(
        default="smart",
        description=(
            "LiteLLM alias for the Pulse Stage-2 LLM scorer (PULSE_STAGE2_MODEL). "
            "Follow-up suggestion: rename to PULSE_LLM_MODEL for clarity."
        ),
    )
    pulse_stage2_max_retries: int = Field(
        default=1,
        description=("Max retry attempts for Pulse Stage-2 LLM calls (PULSE_STAGE2_MAX_RETRIES)."),
    )
    pulse_stage2_timeout_seconds: int = Field(
        default=900,
        description=(
            "Wall-clock timeout for the Stage-2 LLM rerank step in seconds "
            "(PULSE_STAGE2_TIMEOUT_SECONDS).  On expiry the pipeline falls back "
            "to embedding-only ranking and marks the deck as degraded. "
            "Default 900 s is generous for large decks on slow local models."
        ),
    )
    pulse_llm_concurrency: int = Field(
        default=4,
        description=(
            "Max concurrent LLM calls during Stage-2 scoring "
            "(PULSE_LLM_CONCURRENCY).  Effective Ollama parallelism is bounded "
            "by OLLAMA_NUM_PARALLEL (compose default: 2), so values above that "
            "only add queue depth without throughput gain. Default 4 gives a "
            "modest queue buffer without wasteful over-subscription."
        ),
    )

    # --- External API keys ---------------------------------------------
    semantic_scholar_api_key: SecretStr | None = Field(
        default=None,
        description="Semantic Scholar API key (SEMANTIC_SCHOLAR_API_KEY).  Optional.",
    )
    pubmed_api_key: SecretStr | None = Field(
        default=None,
        description="NCBI/PubMed API key (PUBMED_API_KEY).  Optional.",
    )
    ncbi_tool: str = Field(
        default="JARVIS-RD",
        description=(
            "Tool name passed to NCBI E-utilities via the 'tool' query parameter "
            "(NCBI_TOOL).  NCBI best-practice: identify your application so they "
            "can contact you rather than blanket-block.  Default 'JARVIS-RD'."
        ),
    )
    ncbi_email: str = Field(
        default="",
        description=(
            "Contact email passed to NCBI E-utilities via the 'email' query parameter "
            "(NCBI_EMAIL).  NCBI best-practice: provide a valid address for abuse "
            "contact.  Blank = omitted from requests."
        ),
    )
    openalex_api_key: SecretStr | None = Field(
        default=None,
        description="OpenAlex API key (OPENALEX_API_KEY).  Optional.",
    )
    openalex_email: str = Field(
        default="",
        description=(
            "Email address included in OpenAlex API requests for polite-pool "
            "access (OPENALEX_EMAIL).  Blank = anonymous tier."
        ),
    )

    # --- Infrastructure ingest key --------------------------------------
    # infra_ingest_key and infra_ingest_key_file are read via PaperIngestionSettings
    # in routers/infra_events.py.  Mirrors the dual-source pattern used for
    # JARVIS_API_KEY so Docker Secret mounts take precedence.
    infra_ingest_key: SecretStr | None = Field(
        default=None,
        description=(
            "Secret key for the /api/infra-events endpoint (INFRA_INGEST_KEY). "
            "Superseded by INFRA_INGEST_KEY_FILE when both are set."
        ),
    )
    infra_ingest_key_file: str | None = Field(
        default=None,
        description=(
            "Path to a file containing the infra-ingest key "
            "(INFRA_INGEST_KEY_FILE).  Docker Secret mount path."
        ),
    )

    # --- Telegram bot presence -----------------------------------------
    telegram_bot_token: SecretStr | None = Field(
        default=None,
        description=(
            "Telegram bot token (TELEGRAM_BOT_TOKEN).  When set, the system "
            "health endpoint reports Telegram as configured."
        ),
    )


def get_paper_ingestion_settings() -> PaperIngestionSettings:
    """Return a fresh ``PaperIngestionSettings`` snapshot.

    Intentionally uncached so that ``monkeypatch.setenv`` works in tests.
    """
    return PaperIngestionSettings()
