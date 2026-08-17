"""Platform signer boundary for exact Research and Learning commands."""

from __future__ import annotations

from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from jarvis_common.identity_assertions import IdentityAssertionSigner
from jarvis_common.identity_capabilities import (
    IdentityAudience,
    ServicePrincipal,
    service_principal_scopes,
)
from pydantic import BaseModel, Field

from platform_api.deps import authenticate_service_principal, get_db_pool, get_identity_signer

router = APIRouter(prefix="/internal/services", tags=["internal", "services"])
type Principal = Annotated[ServicePrincipal, Depends(authenticate_service_principal)]
type DatabasePool = Annotated[asyncpg.Pool, Depends(get_db_pool)]
type Signer = Annotated[IdentityAssertionSigner, Depends(get_identity_signer)]


class ServiceAuthorizationRequest(BaseModel):
    """One route-bound command request from a service principal."""

    audience: IdentityAudience
    method: str = Field(min_length=1, max_length=16)
    path: str = Field(min_length=1, max_length=2048)
    request_id: str = Field(min_length=1, max_length=128)
    user_id: int = Field(gt=0)


class ServiceAuthorizationResponse(BaseModel):
    """Signed assertion for the one authorized owner command."""

    assertion: str
    scopes: tuple[str, ...]


@router.post("/authorize", response_model=ServiceAuthorizationResponse)
async def authorize_service_command(
    body: ServiceAuthorizationRequest,
    principal: Principal,
    db_pool: DatabasePool,
    signer: Signer,
) -> ServiceAuthorizationResponse:
    """Authenticate a service and mint only its declared owner command."""
    try:
        scopes = service_principal_scopes(principal, body.audience, body.method, body.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Service command binding is invalid") from exc
    if scopes is None:
        raise HTTPException(status_code=403, detail="Service command is not allowed")
    active = await db_pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM users WHERE id = $1 AND deleted_at IS NULL)", body.user_id
    )
    if active is not True:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User is unavailable")
    return ServiceAuthorizationResponse(
        assertion=signer.issue(
            audience=body.audience,
            subject=f"user:{body.user_id}",
            principal=principal,
            user_id=body.user_id,
            request_id=body.request_id,
            request_method=body.method,
            request_path=body.path,
            scopes=scopes,
        ),
        scopes=scopes,
    )


__all__ = ["router"]
