"""Service-principal credential loading and constant-time authentication."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from pathlib import Path

from jarvis_common.identity_capabilities import ServicePrincipal

from platform_api.config import PlatformSettings

_MAX_TOKEN_FILE_BYTES = 4 * 1024
_MIN_TOKEN_LENGTH = 32


class ServicePrincipalConfigurationError(RuntimeError):
    """Raised when a service-principal credential file is unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class ServicePrincipalTokens:
    """In-memory service-principal credentials loaded during startup.

    Parameters
    ----------
    telegram : str
        Telegram service credential.
    research : str
        Research service credential.
    learning : str
        Learning service credential.
    """

    telegram: str
    research: str
    learning: str

    def authenticates(self, principal: ServicePrincipal, presented: str) -> bool:
        """Compare one presented credential in constant time.

        Parameters
        ----------
        principal : {"learning", "research", "telegram"}
            Claimed service identity.
        presented : str
            Credential supplied by the caller.

        Returns
        -------
        bool
            ``True`` only when the credential matches the configured service.
        """
        expected = getattr(self, principal)
        return hmac.compare_digest(presented.encode(), expected.encode())


def load_service_principal_tokens(settings: PlatformSettings) -> ServicePrincipalTokens:
    """Load all service credentials from mandatory Docker-secret files.

    Parameters
    ----------
    settings : PlatformSettings
        Platform settings containing the three credential paths.

    Returns
    -------
    ServicePrincipalTokens
        Validated immutable credentials.

    Raises
    ------
    ServicePrincipalConfigurationError
        If any file is missing, unreadable, symlinked, oversized, empty, or too
        short for a generated service credential.
    """
    return ServicePrincipalTokens(
        telegram=_read_service_token(settings.telegram_service_token_file, "telegram"),
        research=_read_service_token(settings.research_service_token_file, "research"),
        learning=_read_service_token(settings.learning_service_token_file, "learning"),
    )


def _read_service_token(path: Path, principal: str) -> str:
    try:
        if path.is_symlink():
            raise ServicePrincipalConfigurationError(
                f"{principal} service token file must not be a symbolic link"
            )
        size = path.stat().st_size
        if not 1 <= size <= _MAX_TOKEN_FILE_BYTES:
            raise ServicePrincipalConfigurationError(
                f"{principal} service token file must contain 1-{_MAX_TOKEN_FILE_BYTES} bytes"
            )
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ServicePrincipalConfigurationError(
            f"{principal} service token file is unreadable"
        ) from exc
    if len(token) < _MIN_TOKEN_LENGTH:
        raise ServicePrincipalConfigurationError(
            f"{principal} service token must contain at least {_MIN_TOKEN_LENGTH} characters"
        )
    return token


__all__ = [
    "ServicePrincipalConfigurationError",
    "ServicePrincipalTokens",
    "load_service_principal_tokens",
]
