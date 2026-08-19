"""DNS-pinned outbound HTTP and socket connections.

HTTPX has no resolver hook.  This module therefore places a small httpcore
network backend below an HTTPX transport: it resolves a hostname once, checks
the complete answer set, and hands httpcore an IP literal to connect to.  The
request origin remains unchanged, so HTTP Host and TLS SNI/certificate checks
continue to use the original hostname.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import httpcore
import httpx

from jarvis_common.net import is_non_public_address

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[tuple[int, str]]]]
BlockingResolver = Callable[[str, int], list[tuple[int, str]]]

_HTTPCORE_EXCEPTION_MAP: tuple[tuple[type[Exception], type[httpx.RequestError]], ...] = (
    (httpcore.ConnectTimeout, httpx.ConnectTimeout),
    (httpcore.ReadTimeout, httpx.ReadTimeout),
    (httpcore.WriteTimeout, httpx.WriteTimeout),
    (httpcore.PoolTimeout, httpx.PoolTimeout),
    (httpcore.TimeoutException, httpx.TimeoutException),
    (httpcore.ConnectError, httpx.ConnectError),
    (httpcore.ReadError, httpx.ReadError),
    (httpcore.WriteError, httpx.WriteError),
    (httpcore.NetworkError, httpx.NetworkError),
    (httpcore.ProxyError, httpx.ProxyError),
    (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
    (httpcore.LocalProtocolError, httpx.LocalProtocolError),
    (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
    (httpcore.ProtocolError, httpx.ProtocolError),
)


@contextlib.contextmanager
def _map_httpcore_exceptions(request: httpx.Request) -> Iterator[None]:
    """Map httpcore's public exception hierarchy onto HTTPX's public API."""
    try:
        yield
    except Exception as exc:
        for core_type, httpx_type in _HTTPCORE_EXCEPTION_MAP:
            if isinstance(exc, core_type):
                raise httpx_type(str(exc), request=request) from exc
        raise


class PinnedDestinationRejectedError(OSError):
    """The resolved destination violates the selected outbound address policy."""


@dataclass(frozen=True)
class OutboundAddressPolicy:
    """Address policy applied to one DNS result before any connection is made.

    Private addresses are only permitted for exact configured names or literal
    addresses.  Link-local, multicast and unspecified destinations are never
    permitted, including for internal development services and SMTP relays.
    """

    allowed_private_hosts: frozenset[str] = field(default_factory=frozenset)
    allowed_private_addresses: frozenset[str] = field(default_factory=frozenset)

    def allows(self, host: str, address: IPAddress) -> bool:
        if address.is_link_local or address.is_multicast or address.is_unspecified:
            return False
        if not is_non_public_address(address):
            return True
        normalized_host = host.rstrip(".").lower()
        if normalized_host in self.allowed_private_hosts:
            return True
        try:
            literal_host = ipaddress.ip_address(normalized_host)
        except ValueError:
            return False
        return literal_host == address and str(address) in self.allowed_private_addresses


PUBLIC_ONLY = OutboundAddressPolicy()

# Fixed Compose service names, plus the explicit loopback development surface.
# The application services are here because service-to-service calls resolve onto
# the private bridge subnet: the Telegram bot dials paper_ingestion and
# learning_engine, paper ingestion dials the bot for nudge reloads and the vector
# sidecar for its health probe.  Arbitrary remote requests through the shared
# client still take the public-only branch of this policy.
JARVIS_SERVICE_POLICY = OutboundAddressPolicy(
    allowed_private_hosts=frozenset(
        {
            "localhost",
            "ollama",
            "qdrant",
            "litellm",
            "postgres",
            "host.docker.internal",
            "paper_ingestion",
            "platform_api",
            "learning_engine",
            "telegram_bot",
            "vector",
        }
    ),
    allowed_private_addresses=frozenset({"127.0.0.1", "::1"}),
)
LOCAL_DEVELOPMENT_POLICY = OutboundAddressPolicy(
    allowed_private_hosts=frozenset({"localhost"}),
    allowed_private_addresses=frozenset({"127.0.0.1", "::1"}),
)
LITELLM_PROVIDER_POLICY = OutboundAddressPolicy(
    allowed_private_hosts=frozenset({"localhost", "ollama", "vllm", "host.docker.internal"}),
    allowed_private_addresses=frozenset({"127.0.0.1", "::1"}),
)
# Trace export is opt-in and reaches the self-hosted server only.  The
# observability profile publishes it under the Compose service name `langfuse`
# on the private bridge, which is the value LANGFUSE_HOST is documented with, so
# the general service policy would refuse every export.  Any other host the
# operator points LANGFUSE_HOST at must be publicly routable.
LANGFUSE_EXPORT_POLICY = OutboundAddressPolicy(
    allowed_private_hosts=frozenset({"localhost", "langfuse", "host.docker.internal"}),
    allowed_private_addresses=frozenset({"127.0.0.1", "::1"}),
)


def policy_allowing_private_host(host: str) -> OutboundAddressPolicy:
    """Permit private addresses only for one explicit operator-configured host."""
    return OutboundAddressPolicy(allowed_private_hosts=frozenset({host.rstrip(".").lower()}))


def _literal_answer(host: str) -> list[tuple[int, str]] | None:
    """Return the single-family answer for an IP literal, or ``None`` for a name."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return None
    family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
    return [(family, str(literal))]


def _unique_addresses(infos: Iterable[tuple[Any, ...]]) -> list[tuple[int, str]]:
    """Collapse one resolver answer to unique ``(family, address)`` pairs in order."""
    addresses: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for family, _type, _proto, _canonname, sockaddr in infos:
        candidate = (family, sockaddr[0])
        if candidate not in seen:
            seen.add(candidate)
            addresses.append(candidate)
    if not addresses:
        raise httpcore.ConnectError("Unable to resolve host")
    return addresses


async def _resolve_addresses(host: str, port: int) -> list[tuple[int, str]]:
    """Resolve *host* once, preserving the resolver's IPv4/IPv6 order."""
    literal = _literal_answer(host)
    if literal is not None:
        return literal

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise httpcore.ConnectError("Unable to resolve host") from exc
    return _unique_addresses(infos)


def _resolve_addresses_blocking(host: str, port: int) -> list[tuple[int, str]]:
    """Resolve *host* once on the calling thread, preserving the resolver's order."""
    literal = _literal_answer(host)
    if literal is not None:
        return literal

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise httpcore.ConnectError("Unable to resolve host") from exc
    return _unique_addresses(infos)


def _validate_addresses(
    host: str,
    addresses: Iterable[tuple[int, str]],
    policy: OutboundAddressPolicy,
) -> list[tuple[int, str]]:
    """Reject a mixed or forbidden answer set before trying any candidate."""
    validated = list(addresses)
    if not validated:
        raise httpcore.ConnectError("Unable to resolve host")
    for _family, raw_address in validated:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise httpcore.ConnectError("Resolver returned an invalid address") from exc
        if not policy.allows(host, address):
            raise httpcore.ConnectError("Destination is not permitted")
    return validated


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore backend that binds validated DNS answers to the TCP connect."""

    def __init__(
        self,
        policy: OutboundAddressPolicy = PUBLIC_ONLY,
        *,
        resolver: Resolver = _resolve_addresses,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._policy = policy
        self._resolver = resolver
        self._backend = backend or cast(httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend())

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = _validate_addresses(host, await self._resolver(host, port), self._policy)
        last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for _family, address in addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            # A blackholed candidate raises ConnectTimeout, which is a
            # TimeoutException and not a ConnectError; catch it too so a dead
            # first address (e.g. an unreachable IPv6) falls through to the next.
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise httpcore.ConnectError("Unable to connect to destination") from last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._backend.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class PinnedBlockingNetworkBackend(httpcore.NetworkBackend):
    """Blocking counterpart of :class:`PinnedNetworkBackend`.

    Exists for sinks that run on their own thread rather than the event loop,
    such as the OpenTelemetry batch exporter, and applies the same one-shot
    resolution and address policy before any TCP connection.
    """

    def __init__(
        self,
        policy: OutboundAddressPolicy = PUBLIC_ONLY,
        *,
        resolver: BlockingResolver = _resolve_addresses_blocking,
        backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._policy = policy
        self._resolver = resolver
        self._backend = backend or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        addresses = _validate_addresses(host, self._resolver(host, port), self._policy)
        last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for _family, address in addresses:
            # A blackholed candidate raises ConnectTimeout rather than
            # ConnectError, so catch both and fall through to the next address.
            try:
                return self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise httpcore.ConnectError("Unable to connect to destination") from last_error

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        return self._backend.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class _ResponseStream(httpx.AsyncByteStream):
    """Map httpcore response-stream exceptions back to HTTPX's public types."""

    def __init__(self, stream: Any, request: httpx.Request) -> None:
        self._stream = stream
        self._request = request

    async def __aiter__(self):  # type: ignore[override]
        with _map_httpcore_exceptions(self._request):
            async for chunk in self._stream:
                yield chunk

    async def aclose(self) -> None:
        with _map_httpcore_exceptions(self._request):
            await self._stream.aclose()


class PinnedAsyncTransport(httpx.AsyncBaseTransport):
    """HTTPX transport with DNS-pinned socket connections and no proxy support."""

    def __init__(  # noqa: PLR0913 - explicit public transport dependencies
        self,
        policy: OutboundAddressPolicy = PUBLIC_ONLY,
        *,
        verify: ssl.SSLContext | str | bool = True,
        limits: httpx.Limits = httpx.Limits(),
        retries: int = 0,
        resolver: Resolver = _resolve_addresses,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        if verify is False:
            raise ValueError("Pinned transport requires TLS certificate verification")
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=verify, trust_env=False),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            retries=retries,
            network_backend=PinnedNetworkBackend(policy, resolver=resolver, backend=backend),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        import typing

        assert isinstance(request.stream, httpx.AsyncByteStream)
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _map_httpcore_exceptions(request):
            response = await self._pool.handle_async_request(core_request)
        assert isinstance(response.stream, typing.AsyncIterable)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_ResponseStream(response.stream, request),
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


class _BlockingResponseStream(httpx.SyncByteStream):
    """Map httpcore response-stream exceptions back to HTTPX's public types."""

    def __init__(self, stream: Any, request: httpx.Request) -> None:
        self._stream = stream
        self._request = request

    def __iter__(self) -> Iterator[bytes]:
        with _map_httpcore_exceptions(self._request):
            yield from self._stream

    def close(self) -> None:
        with _map_httpcore_exceptions(self._request):
            self._stream.close()


class PinnedBlockingTransport(httpx.BaseTransport):
    """Blocking counterpart of :class:`PinnedAsyncTransport`.

    Parameters
    ----------
    policy : OutboundAddressPolicy
        Address policy applied after pinned DNS resolution.
    resolver : BlockingResolver, optional
        Resolution hook, replaced in tests to bind a destination explicitly.
    """

    def __init__(
        self,
        policy: OutboundAddressPolicy = PUBLIC_ONLY,
        *,
        resolver: BlockingResolver = _resolve_addresses_blocking,
    ) -> None:
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            network_backend=PinnedBlockingNetworkBackend(policy, resolver=resolver),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.SyncByteStream)
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _map_httpcore_exceptions(request):
            response = self._pool.handle_request(core_request)
        assert isinstance(response.stream, Iterable)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_BlockingResponseStream(response.stream, request),
            extensions=response.extensions,
            request=request,
        )

    def close(self) -> None:
        self._pool.close()


def pinned_async_client(
    policy: OutboundAddressPolicy = PUBLIC_ONLY,
    *,
    timeout: httpx.Timeout | float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    headers: dict[str, str] | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """Create a guarded asynchronous HTTP client.

    Parameters
    ----------
    policy : OutboundAddressPolicy
        Address policy applied after pinned DNS resolution.
    timeout : httpx.Timeout or float or None, optional
        Client request timeout.
    transport : httpx.AsyncBaseTransport or None, optional
        Explicit test or production transport. A pinned transport is created
        when omitted.
    headers : dict[str, str] or None, optional
        Default request headers.
    auth : httpx.Auth or None, optional
        HTTPX authentication flow used to mutate or exchange request identity.

    Returns
    -------
    httpx.AsyncClient
        Client with environment proxy inheritance disabled.
    """
    return httpx.AsyncClient(
        transport=transport or PinnedAsyncTransport(policy),
        timeout=timeout,
        trust_env=False,
        headers=headers,
        auth=auth,
    )


async def connect_pinned_socket(
    host: str,
    port: int,
    *,
    policy: OutboundAddressPolicy = PUBLIC_ONLY,
    timeout: float | None = None,
    resolver: Resolver = _resolve_addresses,
) -> socket.socket:
    """Return a connected nonblocking socket after one DNS resolution.

    The caller owns the successful socket. Failed candidates are closed before
    trying the next validated address, and cancellation is intentionally not
    intercepted so the active socket is still closed by the ``finally`` block.
    """
    try:
        addresses = _validate_addresses(host, await resolver(host, port), policy)
    except httpcore.ConnectError as exc:
        if str(exc) == "Destination is not permitted":
            raise PinnedDestinationRejectedError("Destination is not permitted") from exc
        raise OSError("Unable to resolve host") from exc
    loop = asyncio.get_running_loop()
    last_error: OSError | None = None
    for family, address in addresses:
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setblocking(False)
        connected = False
        try:
            sockaddr: tuple[Any, ...] = (
                (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            )
            await asyncio.wait_for(loop.sock_connect(sock, sockaddr), timeout=timeout)
            connected = True
            return sock
        except OSError as exc:
            last_error = exc
        finally:
            if not connected and sock.fileno() != -1:
                sock.close()
    raise OSError("Unable to connect to destination") from last_error
