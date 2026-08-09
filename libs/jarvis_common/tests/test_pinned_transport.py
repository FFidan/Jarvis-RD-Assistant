"""Deterministic DNS-pinning tests for outbound HTTP and socket helpers."""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import socket
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from jarvis_common.pinned_transport import (
    JARVIS_SERVICE_POLICY,
    LITELLM_PROVIDER_POLICY,
    PUBLIC_ONLY,
    OutboundAddressPolicy,
    PinnedAsyncTransport,
    PinnedNetworkBackend,
    _resolve_addresses,
    _ResponseStream,
    connect_pinned_socket,
    pinned_async_client,
)


class _Backend(httpcore.AsyncNetworkBackend):
    def __init__(self, failures: set[str] | None = None) -> None:
        self.hosts: list[str] = []
        self.failures = failures or set()

    async def connect_tcp(self, host: str, port: int, **kwargs):  # type: ignore[no-untyped-def]
        self.hosts.append(host)
        if host in self.failures:
            raise httpcore.ConnectError("candidate unavailable")
        return object()

    async def connect_unix_socket(self, path: str, **kwargs):  # type: ignore[no-untyped-def]
        return object()

    async def sleep(self, seconds: float) -> None:
        return None


async def test_connect_uses_validated_literal_not_hostname() -> None:
    """A rebind cannot reach the hostname's second resolution at connect time."""
    backend = _Backend()

    async def resolver(host: str, port: int) -> list[tuple[int, str]]:
        assert host == "provider.example"
        return [(2, "8.8.8.8")]

    transport = PinnedNetworkBackend(PUBLIC_ONLY, resolver=resolver, backend=backend)
    await transport.connect_tcp("provider.example", 443)

    # Mutation proof: passing ``provider.example`` here would let a delegating
    # backend resolve it again. The pinned backend may delegate only the literal.
    assert backend.hosts == ["8.8.8.8"]


async def test_hostname_delegation_mutation_reaches_the_forbidden_rebind() -> None:
    """The cheapest mutation demonstrates why the delegate must receive a literal."""
    reached: list[str] = []

    class RebindingDelegate(_Backend):
        async def connect_tcp(self, host: str, port: int, **kwargs):  # type: ignore[no-untyped-def]
            reached.append("127.0.0.1" if host == "provider.example" else host)
            return object()

    async def resolver(host: str, port: int) -> list[tuple[int, str]]:
        return [(socket.AF_INET, "8.8.8.8")]

    delegate = RebindingDelegate()
    guarded = PinnedNetworkBackend(PUBLIC_ONLY, resolver=resolver, backend=delegate)
    await guarded.connect_tcp("provider.example", 443)
    assert reached == ["8.8.8.8"]

    # Mutation: delegate the hostname after validation instead of the pinned IP.
    await delegate.connect_tcp("provider.example", 443)
    assert reached[-1] == "127.0.0.1"


async def test_mixed_public_private_answers_fail_before_connect() -> None:
    backend = _Backend()

    async def resolver(host: str, port: int) -> list[tuple[int, str]]:
        return [(2, "8.8.8.8"), (2, "127.0.0.1")]

    transport = PinnedNetworkBackend(PUBLIC_ONLY, resolver=resolver, backend=backend)
    with pytest.raises(httpcore.ConnectError, match="not permitted"):
        await transport.connect_tcp("rebind.example", 443)
    assert backend.hosts == []


async def test_internal_policy_keeps_private_service_route_but_not_metadata() -> None:
    backend = _Backend()

    async def service_resolver(host: str, port: int) -> list[tuple[int, str]]:
        return [(2, "172.20.0.4")]

    transport = PinnedNetworkBackend(
        JARVIS_SERVICE_POLICY, resolver=service_resolver, backend=backend
    )
    await transport.connect_tcp("ollama", 11434)
    assert backend.hosts == ["172.20.0.4"]

    async def metadata_resolver(host: str, port: int) -> list[tuple[int, str]]:
        return [(2, "169.254.169.254")]

    forbidden = PinnedNetworkBackend(
        JARVIS_SERVICE_POLICY, resolver=metadata_resolver, backend=backend
    )
    with pytest.raises(httpcore.ConnectError, match="not permitted"):
        await forbidden.connect_tcp("ollama", 80)


@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
async def test_literal_allowlist_does_not_authorize_a_rebinding_hostname(address: str) -> None:
    backend = _Backend()

    async def resolver(host: str, port: int) -> list[tuple[int, str]]:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, address)]

    transport = PinnedNetworkBackend(JARVIS_SERVICE_POLICY, resolver=resolver, backend=backend)
    with pytest.raises(httpcore.ConnectError, match="not permitted"):
        await transport.connect_tcp("public-provider.example", 443)
    assert backend.hosts == []

    literal = PinnedNetworkBackend(JARVIS_SERVICE_POLICY, resolver=resolver, backend=backend)
    await literal.connect_tcp(address, 443)
    assert backend.hosts == [address]


@pytest.mark.parametrize("host", ["ollama", "vllm", "host.docker.internal"])
def test_litellm_policy_preserves_only_documented_internal_provider_names(host: str) -> None:
    private = ipaddress.ip_address("172.20.0.8")
    assert LITELLM_PROVIDER_POLICY.allows(host, private) is True
    assert LITELLM_PROVIDER_POLICY.allows(f"attacker-{host}.example", private) is False


async def test_candidate_order_and_retry_stay_within_one_validated_answer_set() -> None:
    backend = _Backend(failures={"2001:4860:4860::8888"})

    async def resolver(host: str, port: int) -> list[tuple[int, str]]:
        return [
            (socket.AF_INET6, "2001:4860:4860::8888"),
            (socket.AF_INET, "8.8.8.8"),
        ]

    transport = PinnedNetworkBackend(PUBLIC_ONLY, resolver=resolver, backend=backend)
    await transport.connect_tcp("provider.example", 443)
    assert backend.hosts == ["2001:4860:4860::8888", "8.8.8.8"]


async def test_literal_ip_resolution_does_not_call_system_dns(monkeypatch) -> None:
    loop = asyncio.get_running_loop()

    async def forbidden_getaddrinfo(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("literal address must not use DNS")

    monkeypatch.setattr(loop, "getaddrinfo", forbidden_getaddrinfo)
    assert await _resolve_addresses("2001:4860:4860::8888", 443) == [
        (socket.AF_INET6, "2001:4860:4860::8888")
    ]


class _FakeSocket:
    def __init__(self, family: int) -> None:
        self.family = family
        self.closed = False

    def setblocking(self, _enabled: bool) -> None:
        return None

    def fileno(self) -> int:
        return -1 if self.closed else 10

    def close(self) -> None:
        self.closed = True


async def test_socket_helper_retries_in_order_and_closes_failed_candidate(monkeypatch) -> None:
    sockets: list[_FakeSocket] = []

    def socket_factory(family: int, _kind: int) -> _FakeSocket:
        created = _FakeSocket(family)
        sockets.append(created)
        return created

    async def resolver(host: str, port: int) -> list[tuple[int, str]]:
        return [(socket.AF_INET6, "2001:4860:4860::8888"), (socket.AF_INET, "8.8.8.8")]

    async def sock_connect(_sock: _FakeSocket, address: tuple[Any, ...]) -> None:
        if address[0] == "2001:4860:4860::8888":
            raise OSError("first candidate unavailable")

    loop = asyncio.get_running_loop()
    monkeypatch.setattr("jarvis_common.pinned_transport.socket.socket", socket_factory)
    monkeypatch.setattr(loop, "sock_connect", sock_connect)

    connected = await connect_pinned_socket("provider.example", 443, resolver=resolver)
    assert connected is sockets[1]
    assert sockets[0].closed is True
    assert sockets[1].closed is False


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
async def test_socket_helper_closes_active_candidate_on_timeout_or_cancel(
    monkeypatch, mode
) -> None:
    sockets: list[_FakeSocket] = []
    started = asyncio.Event()

    def socket_factory(family: int, _kind: int) -> _FakeSocket:
        created = _FakeSocket(family)
        sockets.append(created)
        return created

    async def resolver(host: str, port: int) -> list[tuple[int, str]]:
        return [(socket.AF_INET, "8.8.8.8")]

    async def sock_connect(_sock: _FakeSocket, _address: tuple[Any, ...]) -> None:
        started.set()
        await asyncio.Event().wait()

    loop = asyncio.get_running_loop()
    monkeypatch.setattr("jarvis_common.pinned_transport.socket.socket", socket_factory)
    monkeypatch.setattr(loop, "sock_connect", sock_connect)

    if mode == "timeout":
        with pytest.raises(OSError) as exc_info:
            await connect_pinned_socket("provider.example", 443, timeout=0.001, resolver=resolver)
        assert isinstance(exc_info.value.__cause__, TimeoutError)
    else:
        task = asyncio.create_task(
            connect_pinned_socket("provider.example", 443, resolver=resolver)
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(sockets) == 1
    assert sockets[0].closed is True


@asynccontextmanager
async def _http_server(
    records: list[tuple[str, str]],
    connections: list[int],
    *,
    tls: ssl.SSLContext | None = None,
) -> AsyncIterator[int]:
    server: asyncio.AbstractServer

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append(id(writer))
        try:
            while True:
                raw = await reader.readuntil(b"\r\n\r\n")
                lines = raw.decode("ascii").split("\r\n")
                method, target, _version = lines[0].split(" ", 2)
                host = next(line[6:] for line in lines[1:] if line.lower().startswith("host: "))
                records.append((target, host))
                if target == "/redirect":
                    port = server.sockets[0].getsockname()[1]
                    response = (
                        "HTTP/1.1 302 Found\r\n"
                        f"Location: http://second.test:{port}/final\r\n"
                        "Content-Length: 0\r\nConnection: keep-alive\r\n\r\n"
                    ).encode("ascii")
                else:
                    body = f"{method} ok".encode()
                    response = (
                        f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n"
                        "Connection: keep-alive\r\n\r\n"
                    ).encode("ascii") + body
                writer.write(response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=tls)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def _local_policy(*hosts: str) -> OutboundAddressPolicy:
    return OutboundAddressPolicy(allowed_private_hosts=frozenset(hosts))


async def test_http_host_is_preserved_and_connection_is_reused() -> None:
    records: list[tuple[str, str]] = []
    connections: list[int] = []
    resolutions: list[tuple[str, int]] = []

    async def resolver(host: str, port: int) -> list[tuple[int, str]]:
        resolutions.append((host, port))
        return [(socket.AF_INET, "127.0.0.1")]

    async with _http_server(records, connections) as port:
        transport = PinnedAsyncTransport(_local_policy("provider.test"), resolver=resolver)
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            assert (await client.get(f"http://provider.test:{port}/one")).text == "GET ok"
            assert (await client.get(f"http://provider.test:{port}/two")).text == "GET ok"

    assert resolutions == [("provider.test", port)]
    assert len(connections) == 1
    assert records == [("/one", f"provider.test:{port}"), ("/two", f"provider.test:{port}")]


async def test_redirect_repins_each_origin_and_environment_proxy_is_ignored(monkeypatch) -> None:
    records: list[tuple[str, str]] = []
    connections: list[int] = []
    resolutions: list[str] = []

    async def resolver(host: str, port: int) -> list[tuple[int, str]]:
        resolutions.append(host)
        return [(socket.AF_INET, "127.0.0.1")]

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    async with _http_server(records, connections) as port:
        async with pinned_async_client(
            _local_policy("first.test", "second.test"),
            transport=PinnedAsyncTransport(
                _local_policy("first.test", "second.test"), resolver=resolver
            ),
        ) as client:
            response = await client.get(f"http://first.test:{port}/redirect", follow_redirects=True)
            assert response.text == "GET ok"

    assert resolutions == ["first.test", "second.test"]
    assert records == [
        ("/redirect", f"first.test:{port}"),
        ("/final", f"second.test:{port}"),
    ]


def _write_tls_material(directory: Path) -> tuple[Path, Path, Path]:
    now = dt.datetime.now(dt.UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Pinned test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "provider.test")])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("provider.test")]), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = directory / "ca.pem"
    cert_path = directory / "server.pem"
    key_path = directory / "server.key"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


async def test_tls_uses_original_sni_and_rejects_wrong_hostname(tmp_path) -> None:
    ca_path, cert_path, key_path = _write_tls_material(tmp_path)
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(certfile=cert_path, keyfile=key_path)
    observed_sni: list[str | None] = []
    server_tls.set_servername_callback(
        lambda _ssl_socket, server_name, _context: observed_sni.append(server_name)
    )
    client_tls = ssl.create_default_context(cafile=ca_path)
    records: list[tuple[str, str]] = []
    connections: list[int] = []

    async def resolver(host: str, port: int) -> list[tuple[int, str]]:
        return [(socket.AF_INET, "127.0.0.1")]

    async with _http_server(records, connections, tls=server_tls) as port:
        policy = _local_policy("provider.test", "wrong.test")
        async with httpx.AsyncClient(
            transport=PinnedAsyncTransport(policy, resolver=resolver, verify=client_tls),
            trust_env=False,
        ) as client:
            response = await client.get(f"https://provider.test:{port}/tls")
            assert response.status_code == 200
            with pytest.raises(httpx.ConnectError):
                await client.get(f"https://wrong.test:{port}/tls")

    assert records == [("/tls", f"provider.test:{port}")]
    assert observed_sni == ["provider.test", "wrong.test"]


async def test_stream_errors_keep_the_original_request() -> None:
    request = httpx.Request("GET", "https://provider.example/data")

    class FailingStream:
        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            raise httpcore.ReadTimeout("stream timed out")

        async def aclose(self) -> None:
            return None

    stream = _ResponseStream(FailingStream(), request)
    with pytest.raises(httpx.ReadTimeout) as exc_info:
        async for _chunk in stream:
            pass
    assert exc_info.value.request is request


def test_transport_refuses_disabled_certificate_verification() -> None:
    with pytest.raises(ValueError, match="requires TLS certificate verification"):
        PinnedAsyncTransport(verify=False)
