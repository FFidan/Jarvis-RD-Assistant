"""Serialization and type-coercion utilities shared across services."""

from typing import Any


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Interpret user_config JSONB values as booleans."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no", "null", ""):
            return False
    return bool(value)
