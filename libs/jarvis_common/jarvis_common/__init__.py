"""Shared utilities for JARVIS microservices."""

from jarvis_common.app_factory import (
    ServiceLifespanConfig,
    build_database_url,
    configure_lifespan,
    configure_middleware_and_errors,
)
from jarvis_common.audit import log_audit
from jarvis_common.auth import (
    assert_multi_tenant_not_implemented,
    current_user_id,
    current_user_id_or_none,
    current_user_id_with_owner_override,
    validate_production_config,
    verify_api_key,
)
from jarvis_common.crypto import (
    decrypt_secret,
    encrypt_secret,
    mask_secret,
    refresh_fernet_cache,
    resolve_secret_row,
    validate_encrypted_config_rows,
)
from jarvis_common.db_helpers import (
    assert_paper_ownership,
    delete_or_404,
    dynamic_update,
    escape_like,
    fmt_safe,
    get_embed_model,
    get_fast_model,
    get_smart_model,
    init_pg_connection,
    quote_ident,
    validated_model,
    validated_model_with_reason,
)
from jarvis_common.email import send_magic_link
from jarvis_common.error_handlers import (
    generic_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)
from jarvis_common.http_rate_limiter import create_limiter, rate_limit_exceeded_handler
from jarvis_common.jobs import KEEPALIVE_INTERVAL, MAX_STREAM_SECONDS, stream_job_events
from jarvis_common.llm_client import (
    DEFAULT_LITELLM_BASE_URL,
    LLM_TIMEOUT_DEFAULT,
    LLM_TIMEOUT_LONG,
    LLM_TIMEOUT_SHORT,
    ChatCompletionOptions,
    LiteLLMConfig,
    _langfuse_lifespan_hook,
    build_litellm_headers,
    call_llm_structured,
    embed_texts,
    get_litellm_config,
    request_chat_completion_content,
    strip_think_blocks,
    strip_think_streaming,
)
from jarvis_common.logging_config import configure_logging
from jarvis_common.models import (
    ErrorResponse,
    HealthCheckResponse,
    JobCreateResponse,
    JobListResponse,
    JobStatusResponse,
)
from jarvis_common.prompt_safety import escape_llm_text, safe_for_prompt, wrap_delimited
from jarvis_common.request_id import RequestIDMiddleware
from jarvis_common.secrets import read_secret
from jarvis_common.session_middleware import SESSION_COOKIE_NAME, SessionMiddleware
from jarvis_common.source_rate_limiter import SourceRateLimiter
from jarvis_common.streak import compute_streak
from jarvis_common.text_utils import author_matches, normalize_author_name

__all__ = [
    # DRY-002: shared FastAPI app factory
    "ServiceLifespanConfig",
    "build_database_url",
    "configure_lifespan",
    "configure_middleware_and_errors",
    "log_audit",
    "verify_api_key",
    "validate_production_config",
    "current_user_id",
    "current_user_id_or_none",
    "current_user_id_with_owner_override",
    "assert_multi_tenant_not_implemented",
    # DRY-003: crypto helpers re-exported from jarvis_common top-level
    "encrypt_secret",
    "decrypt_secret",
    "mask_secret",
    "refresh_fernet_cache",
    "resolve_secret_row",
    "validate_encrypted_config_rows",
    "SourceRateLimiter",
    "create_limiter",
    "rate_limit_exceeded_handler",
    "assert_paper_ownership",
    "dynamic_update",
    "delete_or_404",
    "escape_like",
    "fmt_safe",
    "init_pg_connection",
    "quote_ident",
    "validated_model",
    "validated_model_with_reason",
    "get_smart_model",
    "get_fast_model",
    "get_embed_model",
    "http_exception_handler",
    "validation_exception_handler",
    "generic_exception_handler",
    "configure_logging",
    "HealthCheckResponse",
    "ErrorResponse",
    "JobCreateResponse",
    "JobListResponse",
    "JobStatusResponse",
    "RequestIDMiddleware",
    "normalize_author_name",
    "author_matches",
    "escape_llm_text",
    "safe_for_prompt",
    "wrap_delimited",
    "ChatCompletionOptions",
    "LiteLLMConfig",
    "_langfuse_lifespan_hook",
    "build_litellm_headers",
    "call_llm_structured",
    "embed_texts",
    "get_litellm_config",
    "request_chat_completion_content",
    "strip_think_blocks",
    "strip_think_streaming",
    "DEFAULT_LITELLM_BASE_URL",
    "LLM_TIMEOUT_DEFAULT",
    "LLM_TIMEOUT_LONG",
    "LLM_TIMEOUT_SHORT",
    "read_secret",
    "send_magic_link",
    "SessionMiddleware",
    "SESSION_COOKIE_NAME",
    "compute_streak",
    "KEEPALIVE_INTERVAL",
    "MAX_STREAM_SECONDS",
    "stream_job_events",
]
