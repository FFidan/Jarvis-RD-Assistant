"""Shared utilities for JARVIS microservices."""

from jarvis_common.auth import validate_production_config, verify_api_key
from jarvis_common.db_helpers import (
    delete_or_404,
    dynamic_update,
    fmt_safe,
    get_embed_model,
    get_fast_model,
    get_smart_model,
    init_pg_connection,
    quote_ident,
    validated_model,
)
from jarvis_common.error_handlers import (
    generic_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)
from jarvis_common.llm_client import (
    DEFAULT_LITELLM_BASE_URL,
    LITELLM_FALLBACK_ENV_NAMES,
    LLM_TIMEOUT_DEFAULT,
    LLM_TIMEOUT_LONG,
    LLM_TIMEOUT_SHORT,
    ChatCompletionOptions,
    LiteLLMConfig,
    build_litellm_headers,
    call_llm,
    call_llm_json_value,
    embed_texts,
    get_litellm_config,
    request_chat_completion_content,
    strip_think_blocks,
)
from jarvis_common.logging_config import configure_logging
from jarvis_common.models import HealthCheckResponse
from jarvis_common.prompt_safety import escape_llm_text, wrap_delimited
from jarvis_common.ratelimit import create_limiter, rate_limit_exceeded_handler
from jarvis_common.request_id import RequestIDMiddleware
from jarvis_common.text_utils import author_matches, normalize_author_name

__all__ = [
    "verify_api_key",
    "validate_production_config",
    "create_limiter",
    "rate_limit_exceeded_handler",
    "dynamic_update",
    "delete_or_404",
    "fmt_safe",
    "init_pg_connection",
    "quote_ident",
    "validated_model",
    "get_smart_model",
    "get_fast_model",
    "get_embed_model",
    "http_exception_handler",
    "validation_exception_handler",
    "generic_exception_handler",
    "configure_logging",
    "HealthCheckResponse",
    "RequestIDMiddleware",
    "normalize_author_name",
    "author_matches",
    "escape_llm_text",
    "wrap_delimited",
    "ChatCompletionOptions",
    "LiteLLMConfig",
    "build_litellm_headers",
    "call_llm",
    "call_llm_json_value",
    "embed_texts",
    "get_litellm_config",
    "request_chat_completion_content",
    "strip_think_blocks",
    "DEFAULT_LITELLM_BASE_URL",
    "LLM_TIMEOUT_DEFAULT",
    "LLM_TIMEOUT_LONG",
    "LLM_TIMEOUT_SHORT",
    "LITELLM_FALLBACK_ENV_NAMES",
]
