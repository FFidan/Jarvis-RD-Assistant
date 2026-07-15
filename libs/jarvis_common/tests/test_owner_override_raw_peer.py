"""M5: X-Owner-User-Id allowlist must check the RAW socket peer, not just XFF.

uvicorn's ProxyHeadersMiddleware (outer layer of the shared stack registered by
``configure_middleware_and_errors``) rewrites ``scope["client"]`` IN PLACE from
``X-Forwarded-For`` before any route dependency runs, so ``request.client.host``
alone reflects a caller-controllable header. ``RawClientStashMiddleware`` is
registered last (= outermost, runs first) and snapshots the original transport
peer under ``RAW_CLIENT_SCOPE_KEY``; the owner-override guard then requires
BOTH the raw peer AND the rewritten client to be allowlisted.

These tests deliberately do NOT monkeypatch ``_ip_in_allowlist`` — the
2026-06-02 lesson is that patching it to ``True`` hid a real bridge-bot 403.
The matrix below drives the REAL middleware stack (built via the real
``configure_middleware_and_errors``) with the REAL CIDR allowlist, controlling
the raw transport peer through ``httpx.ASGITransport(client=...)``:

1. Forged XFF (raw peer = public IP) + valid API key + X-Owner-User-Id → 403.
2. Real bridge peer (no XFF / bridge XFF) + valid key + existing user → 200,
   using the bot's exact header shape (X-API-Key + X-Owner-User-Id).
3. Browser-relayed (XFF public, raw peer allowlisted nginx/loopback) → 403
   with or without the API key.
4. Regression: no-header → None; loopback accept; unknown user → 403;
   stash-absent fallback for apps built without the factory.

Verified identifiers:
- libs/jarvis_common/jarvis_common/auth.py — current_user_id_with_owner_override
  guard (b) requires BOTH _ip_in_allowlist(request.client.host) AND
  _ip_in_allowlist(raw peer from RAW_CLIENT_SCOPE_KEY); fallback to
  request.client when the stash key is absent.
- libs/jarvis_common/jarvis_common/app_factory.py — RawClientStashMiddleware
  registered AFTER ProxyHeadersMiddleware in configure_middleware_and_errors
  (Starlette: last-added = outermost = runs first).
- services/telegram_bot/telegram_bot/handlers/helpers.py:56 — _owner_headers
  sends exactly {"X-API-Key": <key>, "X-Owner-User-Id": str(user_id)}.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from jarvis_common import auth
from jarvis_common.app_factory import configure_middleware_and_errors
from jarvis_common.auth import (
    RAW_CLIENT_SCOPE_KEY,
    current_user_id_with_owner_override,
)
from jarvis_common.http_rate_limiter import create_limiter
from jarvis_common.settings import get_core_settings
from jarvis_common.testing_contract_apps import configure_contract_api_key

# Compose-default allowlist shape: loopback + the jarvis bridge subnet
# (OWNER_OVERRIDE_ALLOWED_CIDRS tracks JARVIS_NET_SUBNET in docker-compose.yml).
BRIDGE_NET = "10.137.241.0/24"
BRIDGE_IP = "10.137.241.5"  # the telegram bot container's socket peer
ATTACKER_IP = "203.0.113.7"  # TEST-NET-3: a public, non-allowlisted peer
BROWSER_IP = "198.51.100.20"  # TEST-NET-2: a public browser behind nginx

OWNER_USER_ID = 42


class _StubUsersPool:
    """Minimal asyncpg.Pool stand-in for guard (c)'s users-existence query.

    Only ``fetchval`` is needed by the guard; the best-effort audit call
    (``log_audit``) swallows the missing ``acquire`` attribute by contract
    ("never raises").
    """

    def __init__(self, *, user_exists: bool = True) -> None:
        self._user_exists = user_exists

    async def fetchval(self, query: str, *args: object) -> int | None:
        return 1 if self._user_exists else None


def _build_factory_app(pool: object, *, trusted_proxy_hosts: str | list[str] = "*") -> FastAPI:
    """Build a real app through configure_middleware_and_errors.

    ``trusted_proxy_hosts="*"`` (the default here) is the most permissive
    proxy-trust setting — exactly the configuration where ProxyHeadersMiddleware
    rewrites ``scope["client"]`` from ANY caller's X-Forwarded-For, i.e. the M5
    attack surface. The P1-01 tests below pass the PRODUCTION value instead
    (``get_core_settings().trusted_proxy_hosts_list``) to prove the deployed
    proxy-trust config actually un-masks nginx-relayed browsers.
    """
    app = FastAPI()
    configure_middleware_and_errors(
        app,
        limiter=create_limiter(default_limits=["10000/minute"], user_aware=False),
        cors_origins=["http://test"],
        trusted_proxy_hosts=trusted_proxy_hosts,
    )
    app.state.db_pool = pool

    @app.get("/whoami")
    async def whoami(
        user_id: int | None = Depends(current_user_id_with_owner_override),
    ) -> dict[str, int | None]:
        return {"user_id": user_id}

    @app.get("/peer")
    async def peer(request: Request) -> dict[str, str | None]:
        raw = request.scope.get(RAW_CLIENT_SCOPE_KEY)
        return {
            "client": request.client.host if request.client else None,
            "raw_client": raw[0] if raw else None,
        }

    return app


def _asgi_client(app: FastAPI, *, raw_peer: str) -> httpx.AsyncClient:
    """ASGI client whose transport reports *raw_peer* as the socket peer."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(raw_peer, 51234)),
        base_url="http://test",
    )


def _bot_headers(api_key: str, user_id: int) -> dict[str, str]:
    """The bot's exact backend-auth header shape.

    Mirrors services/telegram_bot/telegram_bot/handlers/helpers.py:56
    (``_owner_headers``): X-API-Key + X-Owner-User-Id (stringified int),
    nothing else. The ACCEPT case below must keep matching what the real bot
    sends via services_client.py.
    """
    return {"X-API-Key": api_key, "X-Owner-User-Id": str(user_id)}


@pytest.fixture
def bridge_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the REAL compose-shaped allowlist (loopback + bridge subnet)."""
    monkeypatch.setenv("OWNER_OVERRIDE_ALLOWED_CIDRS", f"127.0.0.0/8,{BRIDGE_NET}")
    monkeypatch.setattr(auth, "_CACHED_ALLOWED_NETWORKS", None)
    auth.refresh_allowed_networks_cache()


# ---------------------------------------------------------------------------
# Middleware ordering: the stash must hold the PRE-rewrite socket peer
# ---------------------------------------------------------------------------


async def test_stash_snapshots_raw_peer_before_xff_rewrite() -> None:
    """RawClientStashMiddleware must run BEFORE ProxyHeaders mutates the scope.

    With trusted_proxy_hosts="*" and an XFF header, ProxyHeaders rewrites
    request.client to the XFF-claimed IP — but the stash must still hold the
    transport peer. If the middleware were registered before (= inside)
    ProxyHeadersMiddleware, raw_client would equal the rewritten value and
    this test fails.
    """
    app = _build_factory_app(_StubUsersPool())
    async with _asgi_client(app, raw_peer=ATTACKER_IP) as client:
        resp = await client.get("/peer", headers={"X-Forwarded-For": BRIDGE_IP})
    assert resp.status_code == 200
    body = resp.json()
    assert body["client"] == BRIDGE_IP, "ProxyHeaders should have applied the XFF rewrite"
    assert body["raw_client"] == ATTACKER_IP, (
        "stash must hold the ORIGINAL socket peer, not the XFF-rewritten client"
    )


# ---------------------------------------------------------------------------
# Matrix 1 — forged XFF from a non-allowlisted peer: REJECT
# ---------------------------------------------------------------------------


async def test_forged_xff_with_valid_api_key_is_rejected(
    bridge_allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attacker (public raw peer) forging XFF=bridge + a stolen API key → 403.

    Pre-M5 this was ACCEPTED: request.client.host was already rewritten to the
    XFF-claimed bridge IP, which is allowlisted. The raw-peer requirement now
    rejects it because the actual socket peer is not in the allowlist.
    """
    with configure_contract_api_key(monkeypatch) as key:
        app = _build_factory_app(_StubUsersPool())
        headers = {**_bot_headers(key, OWNER_USER_ID), "X-Forwarded-For": BRIDGE_IP}
        async with _asgi_client(app, raw_peer=ATTACKER_IP) as client:
            resp = await client.get("/whoami", headers=headers)
    assert resp.status_code == 403, (
        f"forged XFF from a public peer must be rejected; got {resp.status_code}: {resp.text}"
    )
    assert "source IP" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Matrix 2 — the bridge bot path: ACCEPT (real allowlist, real middleware)
# ---------------------------------------------------------------------------


async def test_bridge_bot_call_is_accepted(
    bridge_allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bot on the bridge (raw peer allowlisted, no XFF) + key + user → 200.

    This is the non-monkeypatched proof that the bridge-bot path keeps working:
    real middleware stack, real CIDR allowlist, the bot's exact header shape.
    A regression that 403s the bot (the 2026-06-02 production bug class) fails
    here loudly.
    """
    with configure_contract_api_key(monkeypatch) as key:
        app = _build_factory_app(_StubUsersPool(user_exists=True))
        async with _asgi_client(app, raw_peer=BRIDGE_IP) as client:
            resp = await client.get("/whoami", headers=_bot_headers(key, OWNER_USER_ID))
    assert resp.status_code == 200, (
        f"bridge bot must be accepted; got {resp.status_code}: {resp.text}"
    )
    assert resp.json() == {"user_id": OWNER_USER_ID}


async def test_bridge_bot_call_with_bridge_xff_is_accepted(
    bridge_allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bridge raw peer AND bridge XFF (both allowlisted) → still 200."""
    with configure_contract_api_key(monkeypatch) as key:
        app = _build_factory_app(_StubUsersPool(user_exists=True))
        headers = {**_bot_headers(key, OWNER_USER_ID), "X-Forwarded-For": BRIDGE_IP}
        async with _asgi_client(app, raw_peer=BRIDGE_IP) as client:
            resp = await client.get("/whoami", headers=headers)
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    assert resp.json() == {"user_id": OWNER_USER_ID}


# ---------------------------------------------------------------------------
# Matrix 3 — browser-relayed via nginx: REJECT
# ---------------------------------------------------------------------------


async def test_browser_relayed_without_api_key_is_rejected(
    bridge_allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nginx-relayed browser (raw peer = loopback, XFF = public, no API key) → 403.

    Browsers never carry JARVIS_API_KEY, so guard (a) already rejects even
    though the raw peer (nginx on loopback) is allowlisted.
    """
    with configure_contract_api_key(monkeypatch):
        app = _build_factory_app(_StubUsersPool())
        headers = {
            "X-Owner-User-Id": str(OWNER_USER_ID),
            "X-Forwarded-For": BROWSER_IP,
        }
        async with _asgi_client(app, raw_peer="127.0.0.1") as client:
            resp = await client.get("/whoami", headers=headers)
    assert resp.status_code == 403, f"got {resp.status_code}: {resp.text}"
    assert "X-API-Key" in resp.json()["detail"]


async def test_browser_relayed_with_api_key_rejected_by_rewritten_client(
    bridge_allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even WITH a valid key, an XFF-rewritten public client IP → 403.

    The raw peer (nginx/loopback) IS allowlisted here — only the rewritten
    request.client (the browser's public IP) is not. Requiring BOTH keeps
    this path closed, strictly tighter than a raw-peer-only check.
    """
    with configure_contract_api_key(monkeypatch) as key:
        app = _build_factory_app(_StubUsersPool())
        headers = {**_bot_headers(key, OWNER_USER_ID), "X-Forwarded-For": BROWSER_IP}
        async with _asgi_client(app, raw_peer="127.0.0.1") as client:
            resp = await client.get("/whoami", headers=headers)
    assert resp.status_code == 403, f"got {resp.status_code}: {resp.text}"
    assert "source IP" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Matrix 4 — regressions: existing accept/reject semantics preserved
# ---------------------------------------------------------------------------


async def test_no_override_header_resolves_none(
    bridge_allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No X-Owner-User-Id and no session → resolver returns None (no 403)."""
    with configure_contract_api_key(monkeypatch):
        app = _build_factory_app(_StubUsersPool())
        async with _asgi_client(app, raw_peer=ATTACKER_IP) as client:
            resp = await client.get("/whoami")
    assert resp.status_code == 200
    assert resp.json() == {"user_id": None}


async def test_loopback_caller_accepted_with_default_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loopback peer + key + user under the loopback-only CODE default → 200."""
    monkeypatch.delenv("OWNER_OVERRIDE_ALLOWED_CIDRS", raising=False)
    monkeypatch.setattr(auth, "_CACHED_ALLOWED_NETWORKS", None)
    auth.refresh_allowed_networks_cache()
    with configure_contract_api_key(monkeypatch) as key:
        app = _build_factory_app(_StubUsersPool(user_exists=True))
        async with _asgi_client(app, raw_peer="127.0.0.1") as client:
            resp = await client.get("/whoami", headers=_bot_headers(key, OWNER_USER_ID))
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    assert resp.json() == {"user_id": OWNER_USER_ID}


async def test_unknown_user_still_rejected_by_guard_c(
    bridge_allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allowlisted bridge peer + key but a nonexistent user_id → 403 (guard c)."""
    with configure_contract_api_key(monkeypatch) as key:
        app = _build_factory_app(_StubUsersPool(user_exists=False))
        async with _asgi_client(app, raw_peer=BRIDGE_IP) as client:
            resp = await client.get("/whoami", headers=_bot_headers(key, OWNER_USER_ID))
    assert resp.status_code == 403, f"got {resp.status_code}: {resp.text}"
    assert "unknown user" in resp.json()["detail"]


async def test_app_without_stash_middleware_falls_back_to_client_check(
    bridge_allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare app (no factory → no stash, no ProxyHeaders): pre-M5 semantics hold.

    Without configure_middleware_and_errors there is no ProxyHeadersMiddleware
    either, so request.client IS the raw socket peer and the single check on it
    is sufficient — legitimate non-factory apps (and minimal test apps) keep
    working.
    """
    with configure_contract_api_key(monkeypatch) as key:
        app = FastAPI()
        app.state.db_pool = _StubUsersPool(user_exists=True)

        @app.get("/whoami")
        async def whoami(
            user_id: int | None = Depends(current_user_id_with_owner_override),
        ) -> dict[str, int | None]:
            return {"user_id": user_id}

        async with _asgi_client(app, raw_peer=BRIDGE_IP) as client:
            resp = await client.get("/whoami", headers=_bot_headers(key, OWNER_USER_ID))
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    assert resp.json() == {"user_id": OWNER_USER_ID}


# ---------------------------------------------------------------------------
# P1-01 (AC-4) — owner-override proxy-trust bypass: the PRODUCTION proxy-trust
# value (the settings default, NOT "*") must un-mask an nginx-relayed browser.
# Verified: libs/jarvis_common/jarvis_common/settings.py:107 — trusted_proxy_hosts
#   default is now the numeric "127.0.0.0/8,10.137.241.0/24". The pre-fix default
#   was the hostname literal "dashboard", which uvicorn's _TrustedHosts can never
#   match against the numeric bridge peer, so ProxyHeadersMiddleware never
#   rewrote scope["client"] and guard (b) wrongly trusted a relayed browser.
# ---------------------------------------------------------------------------


def _settings_default_proxy_hosts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The proxy-trust list production uses when compose sets no override."""
    monkeypatch.delenv("TRUSTED_PROXY_HOSTS", raising=False)
    return get_core_settings().trusted_proxy_hosts_list


async def test_browser_relayed_rejected_under_production_proxy_trust(
    bridge_allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4 (red→green): a browser relayed through the bridge nginx hop → 403.

    Immediate peer = the trusted bridge proxy (allowlisted), XFF carries a
    PUBLIC browser IP, caller holds the ops key + a valid X-Owner-User-Id.
    Built with the PRODUCTION trusted-proxy value (not "*"),
    ProxyHeadersMiddleware rewrites scope["client"] to the public browser IP, so
    guard (b) rejects it.

    On the base commit the default was the hostname literal "dashboard", which
    never matched the numeric bridge peer: no rewrite fired, scope["client"]
    stayed the allowlisted bridge IP, and the override was WRONGLY resolved
    (200). This assertion fails there and passes after the fix.
    """
    proxy_hosts = _settings_default_proxy_hosts(monkeypatch)
    with configure_contract_api_key(monkeypatch) as key:
        app = _build_factory_app(_StubUsersPool(), trusted_proxy_hosts=proxy_hosts)
        headers = {**_bot_headers(key, OWNER_USER_ID), "X-Forwarded-For": BROWSER_IP}
        async with _asgi_client(app, raw_peer=BRIDGE_IP) as client:
            resp = await client.get("/whoami", headers=headers)
    assert resp.status_code == 403, (
        "an nginx-relayed browser (public XFF) must be rejected under the "
        f"production proxy-trust config; got {resp.status_code}: {resp.text}"
    )
    assert "source IP" in resp.json()["detail"]


async def test_bridge_bot_accepted_under_production_proxy_trust(
    bridge_allowlist: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4 companion: the direct bridge bot still resolves under the SAME
    production proxy-trust value — the fix must not regress the bot.

    Allowlisted bridge peer, NO X-Forwarded-For, ops key + X-Owner-User-Id.
    ProxyHeadersMiddleware has nothing to rewrite, so scope["client"] stays the
    bridge IP and the override resolves.
    """
    proxy_hosts = _settings_default_proxy_hosts(monkeypatch)
    with configure_contract_api_key(monkeypatch) as key:
        app = _build_factory_app(_StubUsersPool(user_exists=True), trusted_proxy_hosts=proxy_hosts)
        async with _asgi_client(app, raw_peer=BRIDGE_IP) as client:
            resp = await client.get("/whoami", headers=_bot_headers(key, OWNER_USER_ID))
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    assert resp.json() == {"user_id": OWNER_USER_ID}
