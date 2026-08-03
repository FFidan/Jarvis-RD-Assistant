"""Snapshot guard for the settings re-export barrel.

``paper_ingestion.services.settings_service`` is a backwards-compatible
re-export shim: the implementations live in submodules (``config_metadata``,
``config_validators``, ``config_db``, ``model_assignment``, …), but a large set
of call sites and ``unittest.mock.patch`` targets still address the symbols
through the barrel namespace.

These snapshots fail loudly if a future refactor silently drops a re-export, so
the call-site-bound patch targets and value imports that depend on the barrel
can't rot without a test catching it. When a re-export is intentionally added
or removed, update the matching expected set below in the same change.
"""

from __future__ import annotations

import paper_ingestion.services.settings_service as settings_service

# Public (non-underscore) names the barrel must continue to re-export.
EXPECTED_PUBLIC_EXPORTS = frozenset(
    {
        "API_KEY_LOGIN_CONFIG_KEY",
        "CLOUD_PROVIDERS",
        "ConfigWriteResult",
        "PERSONAL_KEYS",
        "ProviderTestResult",
        "SYSTEM_KEYS",
        "apply_fetch_interval",
        "apply_pulse_cron",
        "apply_zotero_cron",
        "build_export_zip",
        "cloud_provider_key_present",
        "fetch_papers_by_source",
        "fetch_papers_by_status",
        "migrate_plaintext_secrets",
        "provider_access_configured",
        "reload_telegram_nudges",
        "test_provider_connectivity",
        "validate_custom_openai_base_url",
        "validate_model_assignment",
        "write_config",
    }
)

# Single-underscore (non-dunder) names the barrel must continue to re-export.
# These are the validators / constants / private helpers that tests address via
# the barrel namespace (value imports and ``patch("...settings_service.X")``);
# dropping any of them would make a downstream patch green-but-inert.
EXPECTED_PRIVATE_EXPORTS = frozenset(
    {
        "_ALLOWED_CONFIG_KEYS",
        "_CLOUD_MODEL_PREFIXES",
        "_CONFIG_VALIDATORS",
        "_ENCRYPTED_KEYS",
        "_EXPORT_QUERIES",
        "_MACHINE_ID_RE",
        "_MODEL_ID_RE",
        "_NUDGE_ALLOWED_COLUMNS",
        "_NUDGE_JSONB_COLUMNS",
        "_NUM_CTX_PATTERN",
        "_PULSE_REQUIRED_WEIGHT_KEYS",
        "_PULSE_WEIGHT_KEYS",
        "_ROLE_RE",
        "_SECRET_KEYS",
        "_SOURCE_ALLOWED_COLUMNS",
        "_SOURCE_JSONB_COLUMNS",
        "_SUPPORTED_PROVIDERS",
        "_THINKING_DISABLED_PATTERN",
        "_ZOTERO_LIBRARY_SCOPE_KEYS",
        "_apply_litellm_runtime_update",
        "_classify_config_key",
        "_classify_litellm_runtime_key",
        "_fetch_effective_config_row",
        "_fetch_system_config_values",
        "_is_allowed_config_key",
        "_is_cloud_model_assignment",
        "_log_event",
        "_resolve_config_value",
        "_validate_bool",
        "_validate_cron",
        "_validate_fsrs_learning_steps",
        "_validate_fsrs_retention",
        "_validate_group_id",
        "_validate_l2_lambda",
        "_validate_langfuse_dashboard_url",
        "_validate_library_type",
        "_validate_lookback_days",
        "_validate_nonempty_str",
        "_validate_optional_int",
        "_validate_positive_int",
        "_validate_pulse_weights",
        "_validate_startup_grace_seconds",
        "_validate_zotero_cron",
        "_write_config_row",
    }
)


def _public_names() -> frozenset[str]:
    return frozenset(n for n in dir(settings_service) if not n.startswith("_"))


def _private_names() -> frozenset[str]:
    return frozenset(
        n for n in dir(settings_service) if n.startswith("_") and not n.startswith("__")
    )


def test_barrel_reexports_all_expected_public_names():
    """Every expected public name must still be reachable through the barrel."""
    missing = EXPECTED_PUBLIC_EXPORTS - _public_names()
    assert not missing, f"settings_service barrel dropped public re-exports: {sorted(missing)}"


def test_barrel_reexports_all_expected_private_names():
    """Every expected underscore-prefixed patch/value target must still be present."""
    missing = EXPECTED_PRIVATE_EXPORTS - _private_names()
    assert not missing, f"settings_service barrel dropped private re-exports: {sorted(missing)}"


def test_barrel_public_surface_is_snapshotted():
    """Flag any *new* public re-export so the snapshot stays an accurate contract."""
    unexpected = _public_names() - EXPECTED_PUBLIC_EXPORTS
    assert not unexpected, (
        "settings_service barrel gained unsnapshotted public names "
        f"{sorted(unexpected)}; add them to EXPECTED_PUBLIC_EXPORTS"
    )
