"""Platform-owned setup readiness aggregation."""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import require_admin
from jarvis_common.config_flags import coerce_bool
from jarvis_common.identity_assertions import IdentityAssertionSigner
from jarvis_common.logging_config import request_id_ctx
from jarvis_common.settings import get_secrets_settings
from pydantic import BaseModel, Field, ValidationError

from platform_api.config import get_platform_settings
from platform_api.deps import get_db_pool, get_identity_signer, limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])

_RESEARCH_SETUP_PATH = "/api/system/setup-status/research"
_RESEARCH_SETUP_SCOPES = ("research:system:read",)


class ResearchSetupStatus(BaseModel):
    """Research-owned portion of setup readiness.

    Parameters
    ----------
    models_ready : bool
        Whether the required local models are installed.
    models_downloading : list[str]
        Required model families that are not ready yet.
    topics_count : int
        Number of configured Research topics.
    model_warnings : list[str], optional
        Active model-routing warnings.
    """

    models_ready: bool
    models_downloading: list[str]
    topics_count: int
    model_warnings: list[str] = Field(default_factory=list)


class SetupStatus(BaseModel):
    """Combined setup readiness returned to the dashboard.

    Parameters
    ----------
    setup_completed : bool
        Whether the operator completed first-run setup.
    models_ready : bool
        Whether Research reports the required local models as ready.
    models_downloading : list[str]
        Required model families that are not ready yet.
    topics_count : int
        Number of Research-owned discovery topics.
    telegram_configured : bool
        Whether Platform or the mounted bootstrap secret has a bot token.
    telegram_paired : bool
        Whether Platform owns at least one active Telegram pairing.
    model_warnings : list[str], optional
        Active Research model-routing warnings.
    """

    setup_completed: bool
    models_ready: bool
    models_downloading: list[str]
    topics_count: int
    telegram_configured: bool
    telegram_paired: bool
    model_warnings: list[str] = Field(default_factory=list)


@router.get(
    "/setup-status",
    response_model=SetupStatus,
    dependencies=[Depends(require_admin)],
)
@limiter.limit("30/minute")
async def get_setup_status(
    request: Request,
    pool: Annotated[asyncpg.Pool, Depends(get_db_pool)],
    signer: Annotated[IdentityAssertionSigner, Depends(get_identity_signer)],
) -> SetupStatus:
    """Combine Platform state with Research-owned setup diagnostics.

    Parameters
    ----------
    request : Request
        Authenticated administrator request and shared HTTP client owner.
    pool : asyncpg.Pool
        Platform database pool.
    signer : IdentityAssertionSigner
        Platform-only signer used for the exact Research request.

    Returns
    -------
    SetupStatus
        Point-in-time setup readiness across the two owners.

    Raises
    ------
    HTTPException
        With status 503 when Research cannot provide a valid readiness result.
    """
    async with pool.acquire() as conn:
        setup_row = await conn.fetchrow(
            "SELECT value FROM user_config WHERE key = 'setup.completed' AND user_id IS NULL"
        )
        telegram_paired = bool(
            await conn.fetchval("SELECT EXISTS(SELECT 1 FROM telegram_user_pairings)")
        )
        telegram_token_row = await conn.fetchrow(
            "SELECT value, encrypted_value FROM user_config "
            "WHERE key = 'telegram.bot_token' AND user_id IS NULL"
        )

    stored_token = telegram_token_row is not None and (
        telegram_token_row["value"] is not None or telegram_token_row["encrypted_value"] is not None
    )
    mounted_token = get_secrets_settings().telegram_bot_token
    research = await _get_research_setup_status(request, signer)
    return SetupStatus(
        setup_completed=coerce_bool(
            setup_row["value"] if setup_row is not None else None,
            default=False,
        ),
        models_ready=research.models_ready,
        models_downloading=research.models_downloading,
        topics_count=research.topics_count,
        telegram_configured=stored_token or bool(mounted_token),
        telegram_paired=telegram_paired,
        model_warnings=research.model_warnings,
    )


async def _get_research_setup_status(
    request: Request,
    signer: IdentityAssertionSigner,
) -> ResearchSetupStatus:
    """Fetch the authenticated Research-owned setup projection.

    Parameters
    ----------
    request : Request
        Platform request carrying administrator identity and the HTTP client.
    signer : IdentityAssertionSigner
        Platform assertion signer.

    Returns
    -------
    ResearchSetupStatus
        Validated owner-local readiness projection.

    Raises
    ------
    HTTPException
        With status 503 when Research is unavailable or returns invalid data.
    """
    user_id = _positive_int(getattr(request.state, "user_id", None))
    user_role = getattr(request.state, "user_role", None)
    if user_id is None or user_role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")

    request_id = request_id_ctx.get() or str(uuid.uuid4())
    assertion = signer.issue(
        audience="research",
        subject=f"user:{user_id}",
        principal="browser",
        user_id=user_id,
        user_role=user_role,
        session_id=getattr(request.state, "session_id", None),
        request_id=request_id,
        request_method="GET",
        request_path=_RESEARCH_SETUP_PATH,
        scopes=_RESEARCH_SETUP_SCOPES,
    )
    settings = get_platform_settings()
    client: httpx.AsyncClient = request.app.state.http_client
    try:
        response = await client.get(
            f"{settings.research_api_url}{_RESEARCH_SETUP_PATH}",
            headers={
                "X-Jarvis-Identity": assertion,
                "X-Request-Id": request_id,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        return ResearchSetupStatus.model_validate(response.json())
    except (httpx.HTTPError, ValueError, ValidationError):
        logger.warning("Research setup readiness is unavailable", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Research setup readiness is unavailable",
        ) from None


def _positive_int(value: Any) -> int | None:
    """Return a positive non-boolean integer, otherwise ``None``.

    Parameters
    ----------
    value : Any
        Candidate user identifier.

    Returns
    -------
    int or None
        Valid positive identifier or ``None``.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


__all__ = ["ResearchSetupStatus", "SetupStatus", "get_setup_status", "router"]
