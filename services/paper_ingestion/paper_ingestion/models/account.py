"""Account — self-service current-user profile models.

Strictly the *authenticated caller's own* profile. Admin user-management
(``/api/admin/users``) is a separate surface with its own models; nothing
here grants cross-user read or write.

Note: ``from __future__ import annotations`` is intentionally absent — see
``routers/my_day.py`` / ``docs/plans/2026-04-29-future-import-failure-analysis.md``
for the verified PydanticUserError trace. These models are used as FastAPI
request bodies; their annotations must remain concrete types.
"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field

# RFC 5321 cap. Single source for the email-length limit; routers import
# this rather than redefining it locally.
MAX_EMAIL_LEN = 320
MAX_DISPLAY_NAME_LEN = 120


class AccountResponse(BaseModel):
    """Shape returned by ``GET /api/account`` and ``PATCH /api/account``."""

    id: int
    email: str
    role: str
    display_name: str | None = None
    created_at: datetime
    last_login_at: datetime | None = None


class AccountUpdate(BaseModel):
    """Body for ``PATCH /api/account``.

    Both fields optional:

    - ``display_name`` applies immediately (nullable; empty string clears it).
    - ``email`` does **not** mutate ``users.email`` directly — it triggers a
      verification link to the *new* address; the swap happens only when that
      single-use token is confirmed.
    """

    display_name: Annotated[str, Field(max_length=MAX_DISPLAY_NAME_LEN)] | None = None
    email: Annotated[EmailStr, Field(max_length=MAX_EMAIL_LEN)] | None = None


class AccountUpdateResponse(BaseModel):
    """Result of ``PATCH /api/account``.

    ``account`` always reflects the *current* persisted row (the email is
    unchanged until the verification token is confirmed). ``email_verification_sent``
    is true when an email-change confirmation link was issued.
    """

    account: AccountResponse
    email_verification_sent: bool = False


class ConfirmEmailChangeBody(BaseModel):
    """Body for ``POST /api/account/confirm-email`` — the verify step.

    Mirrors ``paper_ingestion.routers.auth.VerifyBody``: a single-use token
    delivered to the *new* address. Consuming it swaps ``users.email``.
    """

    token: Annotated[str, Field(min_length=16, max_length=128)]


__all__ = [
    "AccountResponse",
    "AccountUpdate",
    "AccountUpdateResponse",
    "ConfirmEmailChangeBody",
]
