"""Passkey ceremony tests — pure-logic units + a real-Postgres contract suite.

The contract suite (``@pytest.mark.contract``) drives the full register/login
ceremonies against a REAL Postgres (the disposable/managed-PG ``contract_conn``
fixture, ``JARVIS_RUN_LIVE_PG=1``) using a minimal in-test software authenticator
(``_SoftAuthenticator``) built on ``cryptography`` + ``cbor2`` — so the SQL that
persists credentials, consumes challenges, revokes sessions, and mints sessions is
exercised end-to-end, not mocked. This is the 3.1 lesson: SQL-bearing security code
needs real-DB assertions.

The pure-logic tests cover the origin allowlist and the signature-counter clone
predicate without a DB (they run under ``-m "not contract"``).
"""

from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace

import cbor2
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from paper_ingestion.routers import auth_passkeys as pk

# ---------------------------------------------------------------------------
# base64url helpers (unpadded, WebAuthn style)
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


# ---------------------------------------------------------------------------
# Pure-logic: origin allowlist
# ---------------------------------------------------------------------------


def _req(origin: str | None) -> SimpleNamespace:
    headers = {} if origin is None else {"origin": origin}
    return SimpleNamespace(headers=headers)


def _no_app_base_url(monkeypatch) -> None:
    monkeypatch.setattr(
        pk, "get_paper_ingestion_settings", lambda: SimpleNamespace(app_base_url=None)
    )


@pytest.mark.parametrize(
    ("origin", "expected_rp_id"),
    [
        ("https://localhost", "localhost"),
        ("https://jarvis.localhost", "jarvis.localhost"),
        # Loopback is accepted on http OR https and on ANY port — the default installer
        # serves plain http://localhost:<DASHBOARD_HOST_PORT> (e.g. :3001), which W3C
        # Secure Contexts treats as a trustworthy context for WebAuthn.
        ("http://localhost:3001", "localhost"),
        ("http://jarvis.localhost", "jarvis.localhost"),
        ("https://localhost:3001", "localhost"),
        ("https://jarvis.localhost:8443", "jarvis.localhost"),
    ],
)
def test_match_origin_allows_loopback(monkeypatch, origin, expected_rp_id):
    _no_app_base_url(monkeypatch)
    match = pk._match_origin(_req(origin))
    assert match.origin == origin  # the full ported origin is used as expected_origin
    assert match.rp_id == expected_rp_id  # rp_id is the port-less hostname


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1",  # raw IP is an invalid rp_id
        "https://127.0.0.1:3001",
        "http://127.0.0.1",  # http does not rescue a raw-IP host
        "https://evil.example.com",
        "http://evil.example.com",  # http is accepted for loopback ONLY, never public hosts
        "https://notlocalhost",  # not a loopback host
        None,
        "",
    ],
)
def test_match_origin_rejects_disallowed(monkeypatch, origin):
    _no_app_base_url(monkeypatch)
    with pytest.raises(pk.HTTPException) as exc:
        pk._match_origin(_req(origin))
    assert exc.value.status_code == 403


def test_match_origin_public_is_exact_match_only(monkeypatch):
    """A configured APP_BASE_URL origin (with port, path stripped) matches EXACTLY.

    Public (reachable) origins get no port/suffix relaxation: a different port on
    the same host is rejected, unlike loopback.
    """
    monkeypatch.setattr(
        pk,
        "get_paper_ingestion_settings",
        lambda: SimpleNamespace(app_base_url="https://jarvis.example.com:8443/app"),
    )
    match = pk._match_origin(_req("https://jarvis.example.com:8443"))
    assert match.rp_id == "jarvis.example.com"
    for bad in (
        "https://jarvis.example.com:9999",
        "https://jarvis.example.com",
        "http://jarvis.example.com:8443",
    ):
        with pytest.raises(pk.HTTPException) as exc:
            pk._match_origin(_req(bad))
        assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "app_base_url",
    ["https://jarvis.example.com:443", "https://jarvis.example.com", "https://jarvis.example.com/"],
)
def test_match_origin_public_default_port_normalized(monkeypatch, app_base_url):
    """An explicit :443 (or none) in APP_BASE_URL matches the browser's port-less Origin."""
    monkeypatch.setattr(
        pk, "get_paper_ingestion_settings", lambda: SimpleNamespace(app_base_url=app_base_url)
    )
    match = pk._match_origin(_req("https://jarvis.example.com"))
    assert match.rp_id == "jarvis.example.com"


@pytest.mark.parametrize(
    "app_base_url",
    [
        "https://h:99999",  # malformed port (fail-closed, no crash)
        "http://jarvis.example.com",  # non-secure scheme: a public passkey origin must be https
        "ftp://jarvis.example.com",
    ],
)
def test_app_base_origin_unusable_values(monkeypatch, app_base_url):
    """A malformed or non-https APP_BASE_URL yields no public origin (fail-closed)."""
    monkeypatch.setattr(
        pk, "get_paper_ingestion_settings", lambda: SimpleNamespace(app_base_url=app_base_url)
    )
    assert pk._app_base_origin() is None


# ---------------------------------------------------------------------------
# Pure-logic: signature-counter clone predicate (WebAuthn L3 §6.1.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "new", "is_clone"),
    [
        (0, 0, False),  # counter-less/synced authenticator — normal
        (0, 1, False),  # first increment from a fresh credential
        (5, 6, False),  # normal increment
        (5, 5, True),  # non-incrementing → clone/replay
        (5, 3, True),  # counter went backwards → clone
        (3, 0, True),  # reset to zero while we hold a nonzero count → clone
    ],
)
def test_is_clone(stored, new, is_clone):
    assert pk._is_clone(stored, new) is is_clone


# ---------------------------------------------------------------------------
# Software authenticator (test double producing real WebAuthn responses)
# ---------------------------------------------------------------------------

_FLAG_UP = 0x01
_FLAG_UV = 0x04
_FLAG_AT = 0x40


class _SoftAuthenticator:
    """A minimal ES256 platform authenticator emitting py_webauthn-verifiable responses."""

    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = hashlib.sha256(str(id(self)).encode()).digest()  # 32 stable bytes
        self.aaguid = b"\x00" * 16

    def _cose_key(self) -> bytes:
        numbers = self._key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _auth_data(self, rp_id: str, *, flags: int, sign_count: int, attested: bool) -> bytes:
        data = (
            hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + sign_count.to_bytes(4, "big")
        )
        if attested:
            cid = self.credential_id
            data += self.aaguid + len(cid).to_bytes(2, "big") + cid + self._cose_key()
        return data

    def register(self, challenge: bytes, origin: str, rp_id: str, *, uv: bool = True) -> dict:
        flags = _FLAG_UP | _FLAG_AT | (_FLAG_UV if uv else 0)
        auth_data = self._auth_data(rp_id, flags=flags, sign_count=0, attested=True)
        att_obj = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cdj = json.dumps(
            {
                "type": "webauthn.create",
                "challenge": _b64(challenge),
                "origin": origin,
                "crossOrigin": False,
            }
        ).encode()
        return {
            "id": _b64(self.credential_id),
            "rawId": _b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64(cdj),
                "attestationObject": _b64(att_obj),
                "transports": ["internal"],
            },
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }

    def authenticate(
        self, challenge: bytes, origin: str, rp_id: str, *, sign_count: int, uv: bool = True
    ) -> dict:
        flags = _FLAG_UP | (_FLAG_UV if uv else 0)
        auth_data = self._auth_data(rp_id, flags=flags, sign_count=sign_count, attested=False)
        cdj = json.dumps(
            {
                "type": "webauthn.get",
                "challenge": _b64(challenge),
                "origin": origin,
                "crossOrigin": False,
            }
        ).encode()
        signature = self._key.sign(
            auth_data + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": _b64(self.credential_id),
            "rawId": _b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64(cdj),
                "authenticatorData": _b64(auth_data),
                "signature": _b64(signature),
                "userHandle": _b64(b"\x00" * 8),
            },
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }


_ORIGIN = "https://localhost"


# ---------------------------------------------------------------------------
# Contract suite (real Postgres)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _passkey_app(contract_conn):
    """PI app wired to the contract connection with the rate limiter disabled."""
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.main import app, limiter

    original = getattr(app.state, "db_pool", None)
    app.state.db_pool = SharedConnPool(contract_conn)
    was_enabled = limiter.enabled
    limiter.enabled = False
    try:
        yield app
    finally:
        limiter.enabled = was_enabled
        if original is None:
            if hasattr(app.state, "db_pool"):
                del app.state.db_pool
        else:
            app.state.db_pool = original


def _client(app, cookie: str | None):
    from jarvis_common.testing_contract_apps import make_contract_client

    return make_contract_client(app, cookie)


async def _register(
    client,
    auth: _SoftAuthenticator,
    *,
    uv: bool = True,
    extra: dict | None = None,
    origin: str = _ORIGIN,
):
    begin = await client.post("/api/auth/passkeys/register/begin", headers={"Origin": origin})
    assert begin.status_code == 200, begin.text
    opts = begin.json()
    cred = auth.register(_b64d(opts["challenge"]), origin, opts["rp"]["id"], uv=uv)
    if extra:
        cred.update(extra)
    return await client.post(
        "/api/auth/passkeys/register/finish", json=cred, headers={"Origin": origin}
    )


async def _login(
    client,
    auth: _SoftAuthenticator,
    *,
    sign_count: int = 1,
    uv: bool = True,
    origin: str = _ORIGIN,
):
    begin = await client.post("/api/auth/passkeys/login/begin", headers={"Origin": origin})
    assert begin.status_code == 200, begin.text
    opts = begin.json()
    assertion = auth.authenticate(
        _b64d(opts["challenge"]), origin, opts["rpId"], sign_count=sign_count, uv=uv
    )
    resp = await client.post(
        "/api/auth/passkeys/login/finish", json=assertion, headers={"Origin": origin}
    )
    return resp, assertion


async def _make_admin(conn, email: str) -> tuple[int, str]:
    from tests.conftest import _seed_user  # canonical seed helper (user + session)

    uid, cookie = await _seed_user(conn, email)
    await conn.execute("UPDATE users SET role = 'admin' WHERE id = $1", uid)
    return uid, cookie


class TestPasskeyCeremonies:
    pytestmark = [
        pytest.mark.contract,
        pytest.mark.real_auth,
        pytest.mark.asyncio(loop_scope="session"),
    ]

    async def test_register_then_login_happy_path(
        self, _passkey_app, contract_conn, _configure_api_key
    ):
        from tests.conftest import _seed_user

        uid, cookie = await _seed_user(contract_conn, "pk-happy@contract.example.com")
        auth = _SoftAuthenticator()

        async with _client(_passkey_app, cookie) as c:
            reg = await _register(c, auth)
        assert reg.status_code == 200, reg.text

        stored_uid = await contract_conn.fetchval(
            "SELECT user_id FROM webauthn_credentials WHERE credential_id = $1", auth.credential_id
        )
        assert stored_uid == uid
        cred_uuid = await contract_conn.fetchval(
            "SELECT id FROM webauthn_credentials WHERE credential_id = $1", auth.credential_id
        )

        async with _client(_passkey_app, None) as c:  # login is unauthenticated
            resp, _ = await _login(c, auth, sign_count=1)
        assert resp.status_code == 200, resp.text
        assert "jarvis_session" in resp.headers.get("set-cookie", "")
        assert resp.json()["id"] == uid

        live = await contract_conn.fetchval(
            "SELECT count(*) FROM sessions WHERE credential_id = $1 AND revoked_at IS NULL",
            cred_uuid,
        )
        assert live == 1

    async def test_register_uv_missing_rejected(
        self, _passkey_app, contract_conn, _configure_api_key
    ):
        from tests.conftest import _seed_user

        _uid, cookie = await _seed_user(contract_conn, "pk-reg-uv@contract.example.com")
        auth = _SoftAuthenticator()
        async with _client(_passkey_app, cookie) as c:
            reg = await _register(c, auth, uv=False)
        assert reg.status_code == 400, reg.text
        count = await contract_conn.fetchval(
            "SELECT count(*) FROM webauthn_credentials WHERE credential_id = $1", auth.credential_id
        )
        assert count == 0

    async def test_login_uv_missing_rejected_no_revoke(
        self, _passkey_app, contract_conn, _configure_api_key
    ):
        from tests.conftest import _seed_user

        _uid, cookie = await _seed_user(contract_conn, "pk-login-uv@contract.example.com")
        auth = _SoftAuthenticator()
        async with _client(_passkey_app, cookie) as c:
            assert (await _register(c, auth)).status_code == 200
        async with _client(_passkey_app, None) as c:
            resp, _ = await _login(c, auth, sign_count=1, uv=False)
        assert resp.status_code == 401, resp.text
        # A UV-false response is an auth failure, NOT a clone — credential survives.
        count = await contract_conn.fetchval(
            "SELECT count(*) FROM webauthn_credentials WHERE credential_id = $1", auth.credential_id
        )
        assert count == 1

    async def test_replayed_challenge_rejected(
        self, _passkey_app, contract_conn, _configure_api_key
    ):
        from tests.conftest import _seed_user

        _uid, cookie = await _seed_user(contract_conn, "pk-replay@contract.example.com")
        auth = _SoftAuthenticator()
        async with _client(_passkey_app, cookie) as c:
            assert (await _register(c, auth)).status_code == 200

        async with _client(_passkey_app, None) as c:
            begin = await c.post("/api/auth/passkeys/login/begin", headers={"Origin": _ORIGIN})
            opts = begin.json()
            assertion = auth.authenticate(
                _b64d(opts["challenge"]), _ORIGIN, opts["rpId"], sign_count=1
            )
            first = await c.post(
                "/api/auth/passkeys/login/finish", json=assertion, headers={"Origin": _ORIGIN}
            )
            second = await c.post(  # same challenge + assertion — single-use claim consumed it
                "/api/auth/passkeys/login/finish", json=assertion, headers={"Origin": _ORIGIN}
            )
        assert first.status_code == 200, first.text
        assert second.status_code == 401, second.text

    async def test_counter_clone_revokes_credential_and_sessions(
        self, _passkey_app, contract_conn, _configure_api_key
    ):
        from tests.conftest import _seed_user

        uid, cookie = await _seed_user(contract_conn, "pk-clone@contract.example.com")
        auth = _SoftAuthenticator()
        async with _client(_passkey_app, cookie) as c:
            assert (await _register(c, auth)).status_code == 200
        cred_uuid = await contract_conn.fetchval(
            "SELECT id FROM webauthn_credentials WHERE credential_id = $1", auth.credential_id
        )
        # Legitimate login advances the stored counter to 5.
        async with _client(_passkey_app, None) as c:
            good, _ = await _login(c, auth, sign_count=5)
        assert good.status_code == 200
        passkey_sid = await contract_conn.fetchval(
            "SELECT id FROM sessions WHERE credential_id = $1", cred_uuid
        )
        # A clone replays a non-incrementing counter (3 <= 5) → reject + revoke.
        async with _client(_passkey_app, None) as c:
            cloned, _ = await _login(c, auth, sign_count=3)
        assert cloned.status_code == 401, cloned.text
        assert (
            await contract_conn.fetchval(
                "SELECT count(*) FROM webauthn_credentials WHERE id = $1", cred_uuid
            )
            == 0
        )
        revoked_at = await contract_conn.fetchval(
            "SELECT revoked_at FROM sessions WHERE id = $1", passkey_sid
        )
        assert revoked_at is not None

    async def test_zero_counter_login_passes(self, _passkey_app, contract_conn, _configure_api_key):
        from tests.conftest import _seed_user

        _uid, cookie = await _seed_user(contract_conn, "pk-zero@contract.example.com")
        auth = _SoftAuthenticator()
        async with _client(_passkey_app, cookie) as c:
            assert (await _register(c, auth)).status_code == 200
        # Authenticator reports sign_count 0 both times (0/0) — normal, not a clone.
        async with _client(_passkey_app, None) as c:
            first, _ = await _login(c, auth, sign_count=0)
            second, _ = await _login(c, auth, sign_count=0)
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text

    async def test_register_unauthenticated_rejected(self, _passkey_app, _configure_api_key):
        async with _client(_passkey_app, None) as c:
            resp = await c.post("/api/auth/passkeys/register/begin", headers={"Origin": _ORIGIN})
        assert resp.status_code == 401, resp.text

    async def test_register_body_user_id_ignored(
        self, _passkey_app, contract_conn, _configure_api_key
    ):
        from tests.conftest import _seed_user

        uid, cookie = await _seed_user(contract_conn, "pk-owner@contract.example.com")
        other_uid, _ = await _seed_user(contract_conn, "pk-victim@contract.example.com")
        auth = _SoftAuthenticator()
        # Attacker-style body: a user_id pointing at another account must be ignored.
        async with _client(_passkey_app, cookie) as c:
            reg = await _register(c, auth, extra={"user_id": other_uid, "nickname": "  my key  "})
        assert reg.status_code == 200, reg.text
        stored_uid = await contract_conn.fetchval(
            "SELECT user_id FROM webauthn_credentials WHERE credential_id = $1", auth.credential_id
        )
        assert stored_uid == uid
        assert reg.json()["nickname"] == "my key"

    async def test_login_soft_deleted_user_rejected(
        self, _passkey_app, contract_conn, _configure_api_key
    ):
        from tests.conftest import _seed_user

        uid, cookie = await _seed_user(contract_conn, "pk-deleted@contract.example.com")
        auth = _SoftAuthenticator()
        async with _client(_passkey_app, cookie) as c:
            assert (await _register(c, auth)).status_code == 200
        cred_uuid = await contract_conn.fetchval(
            "SELECT id FROM webauthn_credentials WHERE credential_id = $1", auth.credential_id
        )
        await contract_conn.execute("UPDATE users SET deleted_at = now() WHERE id = $1", uid)
        async with _client(_passkey_app, None) as c:
            resp, _ = await _login(c, auth, sign_count=1)
        assert resp.status_code == 401, resp.text
        minted = await contract_conn.fetchval(
            "SELECT count(*) FROM sessions WHERE credential_id = $1", cred_uuid
        )
        assert minted == 0  # rejected before mint

    async def test_delete_own_credential_revokes_only_its_sessions(
        self, _passkey_app, contract_conn, _configure_api_key
    ):
        from tests.conftest import _seed_user

        _uid, magic_cookie = await _seed_user(contract_conn, "pk-del@contract.example.com")
        auth = _SoftAuthenticator()
        async with _client(_passkey_app, magic_cookie) as c:
            assert (await _register(c, auth)).status_code == 200
        cred_uuid = await contract_conn.fetchval(
            "SELECT id FROM webauthn_credentials WHERE credential_id = $1", auth.credential_id
        )
        async with _client(_passkey_app, None) as c:
            assert (await _login(c, auth, sign_count=1))[0].status_code == 200
        passkey_sid = await contract_conn.fetchval(
            "SELECT id FROM sessions WHERE credential_id = $1", cred_uuid
        )

        async with _client(_passkey_app, magic_cookie) as c:
            deleted = await c.delete(f"/api/auth/passkeys/{cred_uuid}", headers={"Origin": _ORIGIN})
        assert deleted.status_code == 204, deleted.text
        # The passkey-minted session is revoked; the magic-link session survives.
        assert (
            await contract_conn.fetchval(
                "SELECT revoked_at FROM sessions WHERE id = $1", passkey_sid
            )
            is not None
        )
        assert (
            await contract_conn.fetchval(
                "SELECT revoked_at FROM sessions WHERE id = $1::uuid", magic_cookie
            )
            is None
        )

    async def test_admin_count_and_revoke_all(
        self, _passkey_app, contract_conn, _configure_api_key
    ):
        from tests.conftest import _seed_user

        member_uid, member_cookie = await _seed_user(
            contract_conn, "pk-member@contract.example.com"
        )
        auth = _SoftAuthenticator()
        async with _client(_passkey_app, member_cookie) as c:
            assert (await _register(c, auth)).status_code == 200
        cred_uuid = await contract_conn.fetchval(
            "SELECT id FROM webauthn_credentials WHERE credential_id = $1", auth.credential_id
        )
        async with _client(_passkey_app, None) as c:
            assert (await _login(c, auth, sign_count=1))[0].status_code == 200
        passkey_sid = await contract_conn.fetchval(
            "SELECT id FROM sessions WHERE credential_id = $1", cred_uuid
        )

        _admin_uid, admin_cookie = await _make_admin(contract_conn, "pk-admin@contract.example.com")
        async with _client(_passkey_app, admin_cookie) as c:
            count = await c.get(f"/api/admin/users/{member_uid}/passkeys")
            revoke = await c.post(f"/api/admin/users/{member_uid}/passkeys/revoke-all")
        assert count.status_code == 200 and count.json()["count"] == 1
        assert revoke.status_code == 200, revoke.text
        assert revoke.json()["revoked_credentials"] == 1
        assert revoke.json()["revoked_sessions"] == 1

        assert (
            await contract_conn.fetchval(
                "SELECT count(*) FROM webauthn_credentials WHERE user_id = $1", member_uid
            )
            == 0
        )
        assert (
            await contract_conn.fetchval(
                "SELECT revoked_at FROM sessions WHERE id = $1", passkey_sid
            )
            is not None
        )
        # The member's magic-link session (credential_id NULL) is spared.
        assert (
            await contract_conn.fetchval(
                "SELECT revoked_at FROM sessions WHERE id = $1::uuid", member_cookie
            )
            is None
        )

    async def test_capability_is_post_and_reflects_real_origin(
        self, _passkey_app, _configure_api_key
    ):
        # The probe is POST, not GET: a browser OMITS the Origin header on a same-origin
        # GET, so the capability endpoint must be a POST for the same-origin production
        # request to carry Origin. Modelling that honestly here: NO manual Origin on a
        # GET (which no real browser sends), and the method must be POST-only.
        async with _client(_passkey_app, None) as c:
            no_origin = await c.post("/api/auth/passkeys/capability")
            with_origin = await c.post(
                "/api/auth/passkeys/capability", headers={"Origin": "http://localhost:3001"}
            )
            get_probe = await c.get("/api/auth/passkeys/capability")
        # No Origin (fail-closed) -> not available; a legitimate same-origin POST carries
        # Origin -> available.
        assert no_origin.status_code == 200 and no_origin.json()["available"] is False
        assert with_origin.status_code == 200 and with_origin.json()["available"] is True
        assert "access_mode" in with_origin.json()
        assert get_probe.status_code == 405  # route is POST-only

    @pytest.mark.parametrize(
        ("label", "origin", "app_base_url", "reg_ok"),
        [
            # Default localhost install serves plain http — reachable (rp_id localhost).
            ("localhost", "http://localhost:3001", "", True),
            # Tunnel / custom domain: APP_BASE_URL matches the https Origin -> reachable.
            ("tunnel", "https://jarvis.example.com", "https://jarvis.example.com", True),
            # LAN raw-IP: an IP is an invalid rp_id -> correctly NOT reachable (by design).
            ("lan", "https://192.168.1.5:3001", "", False),
        ],
    )
    async def test_ceremony_reachable_per_access_mode(
        self,
        _passkey_app,
        contract_conn,
        _configure_api_key,
        monkeypatch,
        label,
        origin,
        app_base_url,
        reg_ok,
    ):
        from tests.conftest import _seed_user

        monkeypatch.setattr(
            pk,
            "get_paper_ingestion_settings",
            lambda: SimpleNamespace(app_base_url=app_base_url or None),
        )
        uid, cookie = await _seed_user(contract_conn, f"pk-mode-{label}@contract.example.com")
        auth = _SoftAuthenticator()

        if not reg_ok:
            async with _client(_passkey_app, cookie) as c:
                begin = await c.post(
                    "/api/auth/passkeys/register/begin", headers={"Origin": origin}
                )
            assert begin.status_code == 403, begin.text  # invalid rp_id for a raw-IP origin
            return

        async with _client(_passkey_app, cookie) as c:
            reg = await _register(c, auth, origin=origin)
        assert reg.status_code == 200, reg.text

        async with _client(_passkey_app, None) as c:  # login is unauthenticated
            resp, _ = await _login(c, auth, origin=origin)
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == uid
