"""Tests for the three pydantic-settings Settings classes.

Coverage goals:
* Default values resolve correctly without any env vars set.
* Env-var overrides are picked up when set before instantiation.
* SecretStr fields mask the value in repr but expose it via get_secret_value().
* The monkeypatch-friendly factory functions return fresh instances each call.
* Missing required fields (there are none — all have defaults) don't raise.
* LearningEngineSettings and PaperIngestionSettings inherit JarvisCommonSettings fields.
"""

from __future__ import annotations

import pytest
from jarvis_common.config import JarvisCommonSettings, get_jarvis_common_settings

# ---------------------------------------------------------------------------
# JarvisCommonSettings
# ---------------------------------------------------------------------------


class TestJarvisCommonSettings:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Root conftest disables the gateway boundary for direct-app tests.
        # Remove that explicit test override to verify the production default.
        monkeypatch.delenv("JARVIS_IDENTITY_ASSERTIONS_REQUIRED", raising=False)
        s = JarvisCommonSettings()
        assert s.database_url == ""
        assert s.postgres_user == "jarvis"
        assert s.postgres_db == "jarvis"
        assert s.db_pool_min is None
        assert s.db_pool_max is None
        assert s.cors_origins == "https://localhost:3001"
        assert s.litellm_base_url == "http://litellm:4000"
        assert s.langfuse_host is None
        assert s.trusted_proxy_cidrs == ""
        assert s.trust_cf_connecting_ip is False
        assert s.migration_lock_contended_ok is False
        assert s.observability_enabled is False
        assert s.identity_assertions_required is True
        assert s.identity_issuer == "jarvis-platform"
        assert s.identity_public_key_files == (s.identity_current_public_key_file,)

    def test_identity_rotation_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_IDENTITY_ASSERTIONS_REQUIRED", "true")
        monkeypatch.setenv("JARVIS_IDENTITY_CURRENT_PUBLIC_KEY_FILE", "/keys/current.pem")
        monkeypatch.setenv("JARVIS_IDENTITY_PREVIOUS_PUBLIC_KEY_FILE", "/keys/previous.pem")
        monkeypatch.setenv(
            "JARVIS_IDENTITY_PREVIOUS_KEY_ACCEPT_UNTIL",
            "2026-08-17T12:00:00+00:00",
        )

        settings = JarvisCommonSettings()

        assert settings.identity_assertions_required is True
        assert tuple(str(path) for path in settings.identity_public_key_files) == (
            "/keys/current.pem",
            "/keys/previous.pem",
        )
        assert settings.identity_previous_key_accept_until is not None
        assert settings.identity_previous_key_accept_until.utcoffset() is not None

    def test_database_url_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db")
        s = JarvisCommonSettings()
        assert s.database_url == "postgresql://user:pass@host/db"

    def test_postgres_user_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTGRES_USER", "testuser")
        s = JarvisCommonSettings()
        assert s.postgres_user == "testuser"

    def test_db_pool_min_max(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DB_POOL_MIN", "3")
        monkeypatch.setenv("DB_POOL_MAX", "20")
        s = JarvisCommonSettings()
        assert s.db_pool_min == 3
        assert s.db_pool_max == 20

    def test_cors_origins_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CORS_ORIGINS", "https://a.com, https://b.com , https://c.com")
        s = JarvisCommonSettings()
        assert s.cors_origins_list == ["https://a.com", "https://b.com", "https://c.com"]

    def test_cors_origins_list_single(self) -> None:
        s = JarvisCommonSettings()
        assert s.cors_origins_list == ["https://localhost:3001"]

    def test_langfuse_secret_masked_in_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Langfuse keypair lives on SecretsSettings (not JarvisCommonSettings).

        Verifies SecretStr masking and that LANGFUSE_HOST remains on
        JarvisCommonSettings as a plain non-secret env var.
        """
        from jarvis_common.settings import SecretsSettings, get_secrets_settings

        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "supersecret")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pubkey")
        monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse:3000")
        get_secrets_settings.cache_clear()
        try:
            # Keys now come from SecretsSettings.
            ss = SecretsSettings()
            assert ss.langfuse_secret_key is not None
            assert "supersecret" not in repr(ss.langfuse_secret_key)
            assert ss.langfuse_secret_key.get_secret_value() == "supersecret"
            assert ss.langfuse_public_key is not None
            assert ss.langfuse_public_key.get_secret_value() == "pubkey"
            # Host remains on JarvisCommonSettings as a plain URL.
            s = JarvisCommonSettings()
            assert s.langfuse_host == "http://langfuse:3000"
        finally:
            get_secrets_settings.cache_clear()

    def test_trusted_proxy_cidrs_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "10.0.0.0/8, 172.16.0.0/12")
        s = JarvisCommonSettings()
        assert s.trusted_proxy_cidrs_list == ["10.0.0.0/8", "172.16.0.0/12"]

    def test_trust_cf_connecting_ip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_TRUST_CF_CONNECTING_IP", "true")
        s = JarvisCommonSettings()
        assert s.trust_cf_connecting_ip is True

    def test_migration_lock_contended_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_MIGRATION_LOCK_CONTENDED_OK", "true")
        s = JarvisCommonSettings()
        assert s.migration_lock_contended_ok is True

    def test_factory_returns_fresh_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LITELLM_BASE_URL", "http://custom:9999")
        s = get_jarvis_common_settings()
        assert s.litellm_base_url == "http://custom:9999"
        # Factory is not cached — a second call with a different env reflects that
        monkeypatch.setenv("LITELLM_BASE_URL", "http://other:8888")
        s2 = get_jarvis_common_settings()
        assert s2.litellm_base_url == "http://other:8888"


# ---------------------------------------------------------------------------
# PaperIngestionSettings
# ---------------------------------------------------------------------------


class TestPaperIngestionSettings:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from paper_ingestion.config import PaperIngestionSettings

        for key in (
            "QDRANT_URL",
            "QDRANT_API_KEY",
            "OLLAMA_BASE_URL",
            "VECTOR_API_URL",
            "EMBEDDING_MODEL",
            "EMBEDDING_MODEL_NAME",
            "EMBEDDING_DIMENSION",
        ):
            monkeypatch.delenv(key, raising=False)
        s = PaperIngestionSettings()
        assert s.qdrant_url == "http://qdrant:6333"
        assert s.qdrant_api_key is None
        assert s.ollama_base_url == "http://ollama:11434"
        assert s.vector_api_url == "http://vector:8686"
        assert s.embedding_model == "embed"
        assert s.embedding_model_name == "qwen3-embedding:4b"
        assert s.embedding_dimension == 2560
        assert s.reranker_model == "mixedbread-ai/mxbai-rerank-base-v2"
        assert s.qwen3_reranker_model == "Qwen/Qwen3-Reranker-0.6B"
        assert s.pdf_storage_path == "/data/pdfs"
        assert s.snapshot_storage_path == "/data/snapshots"
        assert s.local_pdf_scan_dir == "/data/local_pdfs"
        assert s.bbt_base_url == "http://host.docker.internal:23119"
        assert s.app_base_url is None
        assert s.auto_fetch_interval_hours == 0.0
        assert s.pulse_stage2_model == "smart"
        assert s.pulse_stage2_max_retries == 1
        assert s.semantic_scholar_api_key is None
        assert s.pubmed_api_key is None
        assert s.openalex_api_key is None
        assert s.infra_ingest_key is None
        assert s.infra_ingest_key_file is None
        assert not hasattr(s, "telegram_bot_token")

    def test_inherits_common_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from paper_ingestion.config import PaperIngestionSettings

        monkeypatch.setenv("DATABASE_URL", "postgresql://x")
        s = PaperIngestionSettings()
        assert s.database_url == "postgresql://x"

    def test_qdrant_api_key_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from paper_ingestion.config import PaperIngestionSettings

        monkeypatch.setenv("QDRANT_API_KEY", "qdrant-secret")
        s = PaperIngestionSettings()
        assert s.qdrant_api_key is not None
        assert "qdrant-secret" not in repr(s.qdrant_api_key)
        assert s.qdrant_api_key.get_secret_value() == "qdrant-secret"

    def test_embedding_dimension_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from paper_ingestion.config import PaperIngestionSettings

        monkeypatch.setenv("EMBEDDING_DIMENSION", "768")
        s = PaperIngestionSettings()
        assert s.embedding_dimension == 768

    def test_auto_fetch_interval_hours(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from paper_ingestion.config import PaperIngestionSettings

        monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "6.5")
        s = PaperIngestionSettings()
        assert s.auto_fetch_interval_hours == pytest.approx(6.5)

    def test_semantic_scholar_api_key_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from paper_ingestion.config import PaperIngestionSettings

        monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "s2-key-123")
        s = PaperIngestionSettings()
        assert s.semantic_scholar_api_key is not None
        assert s.semantic_scholar_api_key.get_secret_value() == "s2-key-123"

    def test_factory_uncached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from paper_ingestion.config import get_paper_ingestion_settings

        monkeypatch.setenv("QDRANT_URL", "http://qdrant:9999")
        s = get_paper_ingestion_settings()
        assert s.qdrant_url == "http://qdrant:9999"
        monkeypatch.setenv("QDRANT_URL", "http://qdrant:7777")
        s2 = get_paper_ingestion_settings()
        assert s2.qdrant_url == "http://qdrant:7777"


# ---------------------------------------------------------------------------
# LearningEngineSettings
# ---------------------------------------------------------------------------


class TestLearningEngineSettings:
    def test_defaults(self) -> None:
        from learning_engine.config import LearningEngineSettings

        s = LearningEngineSettings()
        assert s.snapshot_storage_path == "/data/snapshots"

    def test_inherits_common_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from learning_engine.config import LearningEngineSettings

        monkeypatch.setenv("CORS_ORIGINS", "https://myapp.example.com")
        s = LearningEngineSettings()
        assert s.cors_origins_list == ["https://myapp.example.com"]

    def test_snapshot_storage_path_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from learning_engine.config import LearningEngineSettings

        monkeypatch.setenv("SNAPSHOT_STORAGE_PATH", "/mnt/snapshots")
        s = LearningEngineSettings()
        assert s.snapshot_storage_path == "/mnt/snapshots"

    def test_factory_uncached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from learning_engine.config import get_learning_engine_settings

        monkeypatch.setenv("SNAPSHOT_STORAGE_PATH", "/tmp/snap1")
        s = get_learning_engine_settings()
        assert s.snapshot_storage_path == "/tmp/snap1"
        monkeypatch.setenv("SNAPSHOT_STORAGE_PATH", "/tmp/snap2")
        s2 = get_learning_engine_settings()
        assert s2.snapshot_storage_path == "/tmp/snap2"
