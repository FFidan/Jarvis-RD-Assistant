"""Platform-facing exports for the shared configuration contract."""

from jarvis_common.config_metadata import _ENCRYPTED_KEYS

ENCRYPTED_CONFIG_KEYS = _ENCRYPTED_KEYS

__all__ = ["ENCRYPTED_CONFIG_KEYS"]
