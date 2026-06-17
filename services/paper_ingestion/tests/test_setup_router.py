"""Tests for SetupStatusResponse hw_tier / backend extensions (Task 18)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import paper_ingestion.routers.setup as setup_router
import pytest
from fastapi import HTTPException
from jarvis_common.testing import make_pool_and_conn


@pytest.fixture(autouse=True)
def _disable_limiter():
    from paper_ingestion.deps import limiter

    original = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = original


def _build_request(pool: MagicMock) -> SimpleNamespace:
    state = SimpleNamespace(db_pool=pool)
    app = SimpleNamespace(state=state)
    return SimpleNamespace(app=app, state=state, cookies={})


@pytest.mark.asyncio
async def test_system_check_requires_admin_when_configured() -> None:
    """system_check must raise 403 when an admin exists and caller is not admin."""
    from fastapi import HTTPException

    # admin_count > 0 → setup is complete; caller has no role (unauthenticated)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)  # 1 admin exists
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)
    # request.state has no user_role → non-admin caller

    with pytest.raises(HTTPException) as exc_info:
        await setup_router.system_check(request)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_setup_status_includes_hw_fields(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_HW_TIER", "ge-48")
    monkeypatch.setenv("JARVIS_LLM_BACKEND", "vllm")

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    res = await setup_router.get_status(request)

    assert res.hw_tier_baseline == "ge-48"
    assert res.hw_tier_current is not None
    assert res.current_backend == "vllm"


@pytest.mark.asyncio
async def test_setup_status_reports_effective_backend_when_unset(monkeypatch) -> None:
    """With no JARVIS_LLM_BACKEND override, current_backend reports the effective
    runtime default ('ollama'), not null (OPS-01)."""
    monkeypatch.delenv("JARVIS_LLM_BACKEND", raising=False)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    res = await setup_router.get_status(request)

    assert res.current_backend == "ollama"


@pytest.mark.asyncio
async def test_setup_status_reports_saved_mode_over_env(monkeypatch) -> None:
    """A persisted setup.mode in user_config wins over the env default."""
    monkeypatch.setenv("JARVIS_SETUP_MODE", "single")
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    # get_status reads setup.completed first, then setup.mode.
    pool, _ = make_pool_and_conn(
        conn=conn,
        fetchrow_side_effects=[{"value": True}, {"value": "multi"}],
    )
    request = _build_request(pool)

    res = await setup_router.get_status(request)

    assert res.setup_mode == "multi"


@pytest.mark.asyncio
async def test_setup_status_falls_back_to_env_mode_when_unsaved(monkeypatch) -> None:
    """With no persisted setup.mode row, get_status reports the env default."""
    monkeypatch.setenv("JARVIS_SETUP_MODE", "multi")
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    pool, _ = make_pool_and_conn(
        conn=conn,
        fetchrow_side_effects=[{"value": True}, None],
    )
    request = _build_request(pool)

    res = await setup_router.get_status(request)

    assert res.setup_mode == "multi"


@pytest.mark.asyncio
async def test_setup_status_returns_503_on_db_failure() -> None:
    """get_status must raise HTTP 503 when the DB query fails (fail-closed)."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=asyncpg.PostgresError("connection lost"))
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    with pytest.raises(HTTPException) as exc_info:
        await setup_router.get_status(request)

    assert exc_info.value.status_code == 503
    assert "Setup status check failed" in exc_info.value.detail


# ---------------------------------------------------------------------------
# PI-AUTH-02: first-admin creation log must not contain raw email
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_first_admin_logs_hash_not_raw_email(monkeypatch, caplog) -> None:
    """logger.info on first-admin creation must record email_hash, never the raw address."""
    import hashlib
    import logging

    from fastapi import Response

    raw_email = "admin@example.com"
    expected_hash = hashlib.sha256(raw_email.encode("utf-8")).hexdigest()

    user_row = {"id": 42, "email": raw_email, "role": "admin"}
    conn = AsyncMock()
    # pool.acquire() is called twice: once by require_unconfigured_or_admin (_admin_count),
    # once by the handler body.  Both share the same conn mock.
    # require_unconfigured_or_admin: conn.fetchval → admin_count=0 (bootstrap mode)
    # handler body (inside transaction):
    #   conn.execute  → advisory lock (returns None)
    #   conn.fetchval → admin_count=0 (inner guard)
    #   conn.fetchrow → existing check → None
    #   conn.fetchrow → INSERT RETURNING → user_row
    #   conn.fetchval → session INSERT RETURNING id → 99
    conn.execute = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(side_effect=[0, 0, 99])  # outer count, inner count, session_id
    conn.fetchrow = AsyncMock(side_effect=[None, user_row])  # no existing, INSERT row

    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)
    response = Response()

    body = setup_router.AdminBody(email=raw_email)

    with caplog.at_level(logging.INFO, logger="paper_ingestion.routers.setup"):
        await setup_router.create_first_admin(body, request, response)

    assert any(expected_hash in r.message for r in caplog.records), (
        "Expected email hash in log record"
    )
    assert not any(raw_email in r.message for r in caplog.records), (
        "Raw email must not appear in any log record"
    )


# ---------------------------------------------------------------------------
# F6: configure_cloud_llm_keys delivery hardening
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configure_cloud_llm_keys_uses_config_lock_and_machine_id(monkeypatch):
    """configure_cloud_llm_keys re-push must go through _config_lock and pass machine_id."""

    import paper_ingestion.services.litellm_config as litellm_mod

    # Conn returns the active fast model for the ROLE_TO_ALIAS key lookup.
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=lambda q, *a: (
            {"value": "anthropic/claude-haiku-4-5"} if "llm.fast_model" in a else None
        )
    )
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    # Capture the machine_id passed to update_litellm_model and whether the
    # call happened inside _config_lock.
    captured: list[dict] = []
    lock_held_during: list[bool] = []

    async def fake_update(alias_key, model_id, *, db_pool, machine_id):
        lock_held_during.append(litellm_mod._config_lock.locked())
        captured.append({"alias_key": alias_key, "machine_id": machine_id})
        return True

    monkeypatch.setattr(litellm_mod, "update_litellm_model", fake_update)
    monkeypatch.setattr("paper_ingestion.routers.setup.socket.gethostname", lambda: "test-host")

    # require_unconfigured_or_admin: no admin exists (fetchval = 0)
    conn.fetchval = AsyncMock(return_value=0)
    # Bypass _persist_config entirely — this test is about the delivery plane
    monkeypatch.setattr(
        "paper_ingestion.routers.setup._persist_config", AsyncMock(return_value=None)
    )

    body = setup_router.CloudLlmKeysBody(anthropic="sk-ant-test-key-xxxxxxxxxxxx")
    result = await setup_router.configure_cloud_llm_keys(body, request)

    assert result.restart_required is False
    assert any(c["machine_id"] == "test-host" for c in captured), (
        "machine_id=socket.gethostname() must be passed to update_litellm_model"
    )
    assert all(lock_held_during), "_config_lock must be held during update_litellm_model"


# ---------------------------------------------------------------------------
# SmtpBody field validators
# ---------------------------------------------------------------------------


def test_smtp_body_reply_to_valid_email() -> None:
    """reply_to with a valid email passes validation."""
    body = setup_router.SmtpBody(
        host="smtp.example.com",
        port=587,
        from_email="bot@example.com",
        reply_to="support@example.com",
    )
    assert body.reply_to == "support@example.com"


def test_smtp_body_reply_to_none_passes() -> None:
    """reply_to=None (keep existing) is accepted."""
    body = setup_router.SmtpBody(
        host="smtp.example.com",
        port=587,
        from_email="bot@example.com",
        reply_to=None,
    )
    assert body.reply_to is None


def test_smtp_body_reply_to_empty_string_passes() -> None:
    """reply_to='' (clear) is accepted."""
    body = setup_router.SmtpBody(
        host="smtp.example.com",
        port=587,
        from_email="bot@example.com",
        reply_to="",
    )
    assert body.reply_to == ""


def test_smtp_body_reply_to_invalid_raises_422() -> None:
    """reply_to that is not a valid email raises ValidationError (→ 422)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        setup_router.SmtpBody(
            host="smtp.example.com",
            port=587,
            from_email="bot@example.com",
            reply_to="not-an-email",
        )


def test_smtp_body_from_name_none_passes() -> None:
    """from_name=None (keep existing) is accepted."""
    body = setup_router.SmtpBody(
        host="smtp.example.com",
        port=587,
        from_email="bot@example.com",
        from_name=None,
    )
    assert body.from_name is None


def test_smtp_body_from_name_whitespace_becomes_empty() -> None:
    """Whitespace-only from_name is coerced to '' (clear)."""
    body = setup_router.SmtpBody(
        host="smtp.example.com",
        port=587,
        from_email="bot@example.com",
        from_name="   ",
    )
    assert body.from_name == ""


def test_smtp_body_from_name_with_newline_raises_422() -> None:
    """from_name containing a newline raises ValidationError (→ 422)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        setup_router.SmtpBody(
            host="smtp.example.com",
            port=587,
            from_email="bot@example.com",
            from_name="Evil\nHeader: injected",
        )


def test_smtp_body_from_name_with_carriage_return_raises_422() -> None:
    """from_name containing \\r raises ValidationError (→ 422)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        setup_router.SmtpBody(
            host="smtp.example.com",
            port=587,
            from_email="bot@example.com",
            from_name="JARVIS\rBot",
        )


def test_smtp_body_host_required() -> None:
    """host is required (min_length=1); empty string raises ValidationError."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        setup_router.SmtpBody(
            host="",
            port=587,
            from_email="bot@example.com",
        )


# ---------------------------------------------------------------------------
# SmtpConfigResponse fields
# ---------------------------------------------------------------------------


def test_smtp_config_response_has_reply_to_and_from_name() -> None:
    """SmtpConfigResponse must expose reply_to, from_name, deliverable, issues fields."""
    resp = setup_router.SmtpConfigResponse(
        host="mail.example.com",
        port=587,
        reply_to="support@example.com",
        from_name="JARVIS Bot",
        deliverable=True,
        issues=[],
    )
    assert resp.reply_to == "support@example.com"
    assert resp.from_name == "JARVIS Bot"
    assert resp.deliverable is True
    assert resp.issues == []


# ---------------------------------------------------------------------------
# configure_smtp: persists reply_to and from_name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configure_smtp_persists_reply_to_and_from_name(monkeypatch) -> None:
    """configure_smtp writes smtp.reply_to and smtp.from_name when body values are not None."""
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")

    persisted: dict[str, object] = {}

    async def fake_persist(pool, key, value, *, encrypted):
        persisted[key] = value

    monkeypatch.setattr("paper_ingestion.routers.setup._persist_config", fake_persist)

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)  # no admin → bootstrap
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    body = setup_router.SmtpBody(
        host="smtp.example.com",
        port=587,
        from_email="bot@example.com",
        reply_to="support@example.com",
        from_name="JARVIS Bot",
        test_send=False,
    )
    result = await setup_router.configure_smtp(body, request)

    assert result.saved is True
    assert persisted.get("smtp.reply_to") == "support@example.com"
    assert persisted.get("smtp.from_name") == "JARVIS Bot"


@pytest.mark.asyncio
async def test_configure_smtp_reply_to_none_not_persisted(monkeypatch) -> None:
    """configure_smtp must NOT write smtp.reply_to when body.reply_to is None (keep)."""
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")

    persisted: dict[str, object] = {}

    async def fake_persist(pool, key, value, *, encrypted):
        persisted[key] = value

    monkeypatch.setattr("paper_ingestion.routers.setup._persist_config", fake_persist)

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    body = setup_router.SmtpBody(
        host="smtp.example.com",
        port=587,
        from_email="bot@example.com",
        reply_to=None,  # None → keep existing, do not write
        from_name=None,
        test_send=False,
    )
    await setup_router.configure_smtp(body, request)

    assert "smtp.reply_to" not in persisted, (
        "smtp.reply_to must not be persisted when body.reply_to is None"
    )
    assert "smtp.from_name" not in persisted, (
        "smtp.from_name must not be persisted when body.from_name is None"
    )


@pytest.mark.asyncio
async def test_configure_smtp_empty_reply_to_persisted_as_clear(monkeypatch) -> None:
    """configure_smtp writes '' for smtp.reply_to when body.reply_to is '' (clear)."""
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")

    persisted: dict[str, object] = {}

    async def fake_persist(pool, key, value, *, encrypted):
        persisted[key] = value

    monkeypatch.setattr("paper_ingestion.routers.setup._persist_config", fake_persist)

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    body = setup_router.SmtpBody(
        host="smtp.example.com",
        port=587,
        from_email="bot@example.com",
        reply_to="",  # '' → clear (write it)
        test_send=False,
    )
    await setup_router.configure_smtp(body, request)

    assert "smtp.reply_to" in persisted, "smtp.reply_to must be written when body.reply_to is ''"
    assert persisted["smtp.reply_to"] == ""


# ---------------------------------------------------------------------------
# bootstrap test_send: recipient forced to from_email when no admin exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configure_smtp_bootstrap_test_send_forces_recipient(monkeypatch) -> None:
    """When no admin exists and test_send=True, recipient is forced to from_email."""
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")

    async def fake_persist(pool, key, value, *, encrypted):
        pass

    monkeypatch.setattr("paper_ingestion.routers.setup._persist_config", fake_persist)

    captured_recipient: list[str] = []

    async def fake_send_test(body, recipient, password):
        captured_recipient.append(recipient)
        return None  # success

    monkeypatch.setattr("paper_ingestion.routers.setup._send_test_email", fake_send_test)

    from jarvis_common.email import _EffectiveSmtp

    async def fake_effective_smtp(pool):
        return _EffectiveSmtp(
            host="smtp.example.com", port=587, user=None, password=None, sender="bot@example.com"
        )

    monkeypatch.setattr("paper_ingestion.routers.setup._effective_smtp", fake_effective_smtp)

    conn = AsyncMock()
    # fetchval is called twice: once in require_unconfigured_or_admin, once in the
    # _admin_count check before choosing recipient.
    conn.fetchval = AsyncMock(return_value=0)  # no admin
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    body = setup_router.SmtpBody(
        host="smtp.example.com",
        port=587,
        from_email="bot@example.com",
        test_send=True,
        test_recipient="different-recipient@example.com",  # must be IGNORED in bootstrap
    )
    result = await setup_router.configure_smtp(body, request)

    assert result.test_sent is True
    assert len(captured_recipient) == 1
    assert captured_recipient[0] == "bot@example.com", (
        f"Bootstrap mode must force recipient=from_email; got {captured_recipient[0]!r}"
    )


# ---------------------------------------------------------------------------
# S1 (F1): /api/system/readiness reports SMTP via the DB-aware probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_readiness_smtp_check_present_and_db_aware(monkeypatch) -> None:
    """get_system_readiness returns 200 with an SMTP check resolved from the effective relay.

    Regression guard for the S1 fix: the readiness builder must await the
    DB-aware ``smtp_configured`` probe (not just read the env var), and the
    SMTP check must be present and green when a relay is configured via
    ``user_config`` even with the env unset.
    """
    from types import SimpleNamespace

    import paper_ingestion.routers.system as system_router
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    get_secrets_settings.cache_clear()

    # _effective_smtp reads user_config rows (smtp.host/from set → deliverable);
    # the audit_log count uses conn.fetchrow.
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[
            {"key": "smtp.host", "value": "mail.example.com", "encrypted_value": None},
            {"key": "smtp.from", "value": "bot@example.com", "encrypted_value": None},
        ]
    )
    conn.fetchrow = AsyncMock(return_value={"n": 0})
    pool, _ = make_pool_and_conn(conn=conn)

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)),
        headers={"x-forwarded-proto": "https"},
        url=SimpleNamespace(scheme="https"),
    )

    res = await system_router.get_system_readiness(request)

    get_secrets_settings.cache_clear()

    smtp_checks = [c for c in res.checks if c.name == "smtp"]
    assert len(smtp_checks) == 1, "readiness must include exactly one smtp check"
    assert smtp_checks[0].status == "green", (
        f"DB-configured relay must report green; got {smtp_checks[0].status!r} "
        f"({smtp_checks[0].detail!r})"
    )


# ---------------------------------------------------------------------------
# S1 (F1): test_send uses the stored password when body.password is blank
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configure_smtp_test_send_uses_stored_password_when_blank(monkeypatch) -> None:
    """When body.password is blank but a password is stored, the test send uses the stored one."""
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")

    async def fake_persist(pool, key, value, *, encrypted):
        pass

    monkeypatch.setattr("paper_ingestion.routers.setup._persist_config", fake_persist)

    captured_password: list[str | None] = []

    async def fake_send_test(body, recipient, password):
        captured_password.append(password)
        return None  # success

    monkeypatch.setattr("paper_ingestion.routers.setup._send_test_email", fake_send_test)

    from jarvis_common.email import _EffectiveSmtp

    async def fake_effective_smtp(pool):
        return _EffectiveSmtp(
            host="smtp.example.com",
            port=587,
            user="relay-user",
            password="STORED_SECRET",
            sender="bot@example.com",
        )

    monkeypatch.setattr("paper_ingestion.routers.setup._effective_smtp", fake_effective_smtp)

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)  # no admin → bootstrap
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    body = setup_router.SmtpBody(
        host="smtp.example.com",
        port=587,
        from_email="bot@example.com",
        user="relay-user",
        test_send=True,
    )
    result = await setup_router.configure_smtp(body, request)

    assert result.test_sent is True
    assert captured_password == ["STORED_SECRET"], (
        f"blank body.password must resolve to the stored password; got {captured_password!r}"
    )


@pytest.mark.asyncio
async def test_configure_smtp_test_send_uses_body_password_when_provided(monkeypatch) -> None:
    """When body.password is provided, the test send uses it (not the stored one)."""
    monkeypatch.setenv("ALLOW_PRIVATE_SMTP_HOST", "true")

    async def fake_persist(pool, key, value, *, encrypted):
        pass

    monkeypatch.setattr("paper_ingestion.routers.setup._persist_config", fake_persist)

    captured_password: list[str | None] = []

    async def fake_send_test(body, recipient, password):
        captured_password.append(password)
        return None

    monkeypatch.setattr("paper_ingestion.routers.setup._send_test_email", fake_send_test)

    # _effective_smtp must NOT be consulted when body.password is present.
    async def fail_effective_smtp(pool):  # pragma: no cover
        raise AssertionError("_effective_smtp must not be called when body.password is provided")

    monkeypatch.setattr("paper_ingestion.routers.setup._effective_smtp", fail_effective_smtp)

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    body = setup_router.SmtpBody(
        host="smtp.example.com",
        port=587,
        from_email="bot@example.com",
        user="relay-user",
        password="TYPED_SECRET",
        test_send=True,
    )
    await setup_router.configure_smtp(body, request)

    assert captured_password == ["TYPED_SECRET"]


# ---------------------------------------------------------------------------
# _classify_config_key and _SECRET_KEYS
# ---------------------------------------------------------------------------


def test_classify_config_key_smtp_reply_to_is_system() -> None:
    from paper_ingestion.services.config_metadata import _classify_config_key

    assert _classify_config_key("smtp.reply_to") == "system"


def test_classify_config_key_smtp_from_name_is_system() -> None:
    from paper_ingestion.services.config_metadata import _classify_config_key

    assert _classify_config_key("smtp.from_name") == "system"


def test_smtp_reply_to_not_in_secret_keys() -> None:
    from paper_ingestion.services.config_metadata import _SECRET_KEYS

    assert "smtp.reply_to" not in _SECRET_KEYS


def test_smtp_from_name_not_in_secret_keys() -> None:
    from paper_ingestion.services.config_metadata import _SECRET_KEYS

    assert "smtp.from_name" not in _SECRET_KEYS


# ---------------------------------------------------------------------------
# _validate_optional_email and _validate_optional_header_str
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, ""])
def test_validate_optional_email_allows_none_and_empty(value) -> None:
    from paper_ingestion.services.config_validators import _validate_optional_email

    # Must not raise
    _validate_optional_email(value)


@pytest.mark.parametrize("value", ["support@example.com", "a@b.io"])
def test_validate_optional_email_valid_email(value) -> None:
    from paper_ingestion.services.config_validators import _validate_optional_email

    _validate_optional_email(value)  # must not raise


def test_validate_optional_email_rejects_invalid() -> None:
    from paper_ingestion.services.config_validators import _validate_optional_email

    with pytest.raises(ValueError):
        _validate_optional_email("not-an-email")


@pytest.mark.parametrize("value", [None, ""])
def test_validate_optional_header_str_allows_none_and_empty(value) -> None:
    from paper_ingestion.services.config_validators import _validate_optional_header_str

    _validate_optional_header_str(value)  # must not raise


def test_validate_optional_header_str_valid_string() -> None:
    from paper_ingestion.services.config_validators import _validate_optional_header_str

    _validate_optional_header_str("JARVIS Bot")  # must not raise


@pytest.mark.parametrize("value", ["Evil\r\nBcc: x@y.com", "has\nnewline", "has\x00null"])
def test_validate_optional_header_str_rejects_control_chars(value) -> None:
    from paper_ingestion.services.config_validators import _validate_optional_header_str

    with pytest.raises(ValueError):
        _validate_optional_header_str(value)


# ---------------------------------------------------------------------------
# GET /api/setup/smtp returns reply_to, from_name, deliverable, issues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_smtp_config_returns_reply_to_and_from_name(monkeypatch) -> None:
    """GET /api/setup/smtp returns reply_to, from_name, deliverable, issues fields."""
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)

    from jarvis_common.settings import get_secrets_settings

    get_secrets_settings.cache_clear()

    conn = AsyncMock()
    # _admin_count (require_unconfigured_or_admin) → 0 (bootstrap)
    conn.fetchval = AsyncMock(return_value=0)
    # _read_smtp_config fetches all smtp.* rows
    conn.fetch = AsyncMock(
        return_value=[
            {"key": "smtp.host", "value": "mail.example.com", "encrypted_value": None},
            {"key": "smtp.port", "value": "587", "encrypted_value": None},
            {"key": "smtp.from", "value": "bot@example.com", "encrypted_value": None},
            {"key": "smtp.reply_to", "value": "support@example.com", "encrypted_value": None},
            {"key": "smtp.from_name", "value": "JARVIS Bot", "encrypted_value": None},
        ]
    )
    # _effective_smtp inside effective_smtp_status also calls pool.acquire/conn.fetch
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    result = await setup_router.get_smtp_config(request)

    get_secrets_settings.cache_clear()
    assert result.reply_to == "support@example.com", f"reply_to mismatch: {result.reply_to!r}"
    assert result.from_name == "JARVIS Bot", f"from_name mismatch: {result.from_name!r}"
    assert hasattr(result, "deliverable"), "SmtpConfigResponse must have 'deliverable' field"
    assert hasattr(result, "issues"), "SmtpConfigResponse must have 'issues' field"


@pytest.mark.asyncio
async def test_configure_cloud_llm_keys_push_failure_no_restart_required(monkeypatch):
    """A failed live push must NOT set restart_required — reconciler retries in ≤30 s."""
    import paper_ingestion.services.litellm_config as litellm_mod

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=lambda q, *a: {"value": "openai/gpt-4o"} if "llm.smart_model" in a else None
    )
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock(return_value=None)
    pool, _ = make_pool_and_conn(conn=conn)
    request = _build_request(pool)

    async def failing_update(alias_key, model_id, *, db_pool, machine_id):
        raise RuntimeError("LiteLLM unreachable")

    monkeypatch.setattr(litellm_mod, "update_litellm_model", failing_update)
    monkeypatch.setattr("paper_ingestion.routers.setup.socket.gethostname", lambda: "test-host")
    monkeypatch.setattr(
        "paper_ingestion.routers.setup._persist_config", AsyncMock(return_value=None)
    )

    body = setup_router.CloudLlmKeysBody(openai="sk-openai-test-key-xxxxxxxxxxxx")
    result = await setup_router.configure_cloud_llm_keys(body, request)

    assert result.restart_required is False
    assert result.applied_now == []
