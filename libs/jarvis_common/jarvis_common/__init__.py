"""Shared utilities for JARVIS microservices."""

from jarvis_common.app_factory import (
    ServiceLifespanConfig,
    configure_lifespan,
    configure_middleware_and_errors,
)
from jarvis_common.audit import log_audit
from jarvis_common.auth import (
    current_user_id,  # noqa: F401 — kept for backward compat; prefer current_user_id_or_none
    current_user_id_or_none,
    current_user_id_strict,
    current_user_id_strict_with_owner_override,
    get_current_user_id,
    get_current_user_id_or_bot,
    require_admin,
    require_admin_or_api_key,
    verify_api_key,
)
from jarvis_common.db_helpers import (
    assert_paper_ownership,
    assert_papers_ownership,
    delete_or_404,
    dynamic_update,
    effective_num_ctx,
    escape_like,
    get_fast_model,
    get_smart_model,
    init_pg_connection,
    invalidate_effective_num_ctx_cache,
    validated_model,
)
from jarvis_common.health import register_health_routes
from jarvis_common.http_rate_limiter import create_limiter
from jarvis_common.logging_config import configure_logging
from jarvis_common.models import (
    ErrorResponse,
    HealthCheckResponse,
    JobCreateResponse,
    JobStatusResponse,
)
from jarvis_common.sentry import maybe_init_sentry
from jarvis_common.text_utils import author_matches

__all__ = [
    # DRY-002: shared FastAPI app factory
    "ServiceLifespanConfig",
    "configure_lifespan",
    "configure_middleware_and_errors",
    # Shared health-check routes
    "register_health_routes",
    "log_audit",
    "verify_api_key",
    "current_user_id_or_none",
    "current_user_id_strict",
    "current_user_id_strict_with_owner_override",
    "get_current_user_id",
    "get_current_user_id_or_bot",
    "require_admin",
    "require_admin_or_api_key",
    "create_limiter",
    "assert_paper_ownership",
    "assert_papers_ownership",
    "dynamic_update",
    "effective_num_ctx",
    "delete_or_404",
    "escape_like",
    "init_pg_connection",
    "invalidate_effective_num_ctx_cache",
    "validated_model",
    "get_smart_model",
    "get_fast_model",
    "configure_logging",
    "HealthCheckResponse",
    "ErrorResponse",
    "JobCreateResponse",
    "JobStatusResponse",
    "author_matches",
    "maybe_init_sentry",
]
