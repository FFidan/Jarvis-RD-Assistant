"""Platform API application for identity and operator-owned state."""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI
from jarvis_common import (
    ServiceLifespanConfig,
    configure_lifespan,
    configure_logging,
    configure_middleware_and_errors,
    maybe_init_sentry,
    register_health_routes,
)
from jarvis_common.health import make_postgres_probe
from jarvis_common.identity_keys import load_identity_signer
from jarvis_common.session_middleware import SessionMiddleware
from jarvis_common.settings import get_core_settings
from jarvis_common.version import app_version

from platform_api.auth_cookie_relay import AuthCookieRelayMiddleware
from platform_api.config import get_platform_settings
from platform_api.deps import limiter, verify_platform_request
from platform_api.routers import (
    account,
    admin,
    audit_admin,
    auth,
    auth_passkeys,
    configuration,
    erasure,
    internal_auth,
    internal_services,
    internal_telegram,
    jobs,
    logs,
    providers,
    setup,
    system,
    telegram,
)
from platform_api.service_principals import load_service_principal_tokens
from platform_api.services.config_delivery import start_reconciler, stop_reconciler
from platform_api.services.erasure import start_coordinator, stop_coordinator

configure_logging("platform_api", log_level=get_core_settings().log_level)
maybe_init_sentry("platform_api")

logger = logging.getLogger(__name__)


async def _load_identity_signer(app: FastAPI) -> None:
    """Load the Platform-only Ed25519 signer during application startup.

    Parameters
    ----------
    app : FastAPI
        Application whose state receives the configured signer.
    """
    settings = get_platform_settings()
    app.state.identity_signer = load_identity_signer(
        settings.identity_private_key_file,
        issuer=settings.identity_issuer,
    )


async def _load_service_principals(app: FastAPI) -> None:
    """Load dedicated service credentials during application startup.

    Parameters
    ----------
    app : FastAPI
        Application whose state receives the credential snapshot.
    """
    app.state.service_principal_tokens = load_service_principal_tokens(get_platform_settings())


async def _migrate_plaintext_secrets(app: FastAPI) -> None:
    """Re-encrypt legacy plaintext Platform secrets without blocking startup.

    Parameters
    ----------
    app : FastAPI
        Platform application whose database pool owns ``user_config``.
    """
    from jarvis_common.config_store import migrate_plaintext_secrets  # noqa: PLC0415

    try:
        await migrate_plaintext_secrets(app.state.db_pool)
    except Exception:
        logger.warning("Platform secret migration failed during startup", exc_info=True)


_lifespan_config = ServiceLifespanConfig(
    service_name="Platform API",
    custom_init_tasks=[
        _load_identity_signer,
        _load_service_principals,
        _migrate_plaintext_secrets,
        start_coordinator,
        start_reconciler,
    ],
    custom_teardown_tasks=[None, None, None, stop_coordinator, stop_reconciler],
)

app = FastAPI(
    title="JARVIS Platform API",
    description="Identity, configuration, pairing, and operator controls",
    version=app_version(),
    lifespan=configure_lifespan(_lifespan_config),
    dependencies=[Depends(verify_platform_request)],
)

configure_middleware_and_errors(
    app,
    limiter=limiter,
    trusted_proxy_hosts=get_core_settings().trusted_proxy_hosts_list,
)
app.add_middleware(SessionMiddleware)
app.add_middleware(AuthCookieRelayMiddleware)

for platform_router in (
    internal_auth.router,
    internal_services.router,
    erasure.router,
    internal_telegram.router,
    jobs.router,
    configuration.router,
    logs.router,
    providers.router,
    auth.router,
    auth_passkeys.router,
    admin.router,
    audit_admin.router,
    setup.router,
    system.router,
    telegram.router,
    account.router,
):
    app.include_router(platform_router)

register_health_routes(
    app,
    service_name="platform_api",
    checks=[("postgres", make_postgres_probe())],
    limiter=limiter,
)


__all__ = ["app"]
