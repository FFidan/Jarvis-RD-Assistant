"""Shared utilities for JARVIS microservices.

**Internal to this application. Not a supported library.** This package is never
published to a package index; the service images install it from the path
(`COPY libs/jarvis_common/`), so its only consumers are the services in this
repository. The top-level namespace is an implementation detail, not a
compatibility promise: names may be added, moved or removed in any release,
including a patch release, and the package version is not a semantic-versioning
contract for out-of-tree callers.

``__all__`` exists to keep that surface minimal and honest, not to advertise it.
``tests/test_public_api.py`` pins it to the names the services actually import
from the top level.
"""

from jarvis_common.app_factory import (
    ServiceLifespanConfig,
    configure_lifespan,
    configure_middleware_and_errors,
)
from jarvis_common.audit import log_audit
from jarvis_common.auth import (
    current_user_id_or_none,
    current_user_id_strict,
    get_current_user_id,
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
    "get_current_user_id",
    "require_admin",
    "require_admin_or_api_key",
    "create_limiter",
    "assert_paper_ownership",
    "assert_papers_ownership",
    "dynamic_update",
    "effective_num_ctx",
    "delete_or_404",
    "escape_like",
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
