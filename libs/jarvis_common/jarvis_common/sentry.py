"""Initialize Sentry without default PII and enforce outbound quarantine.

Telemetry remains disabled when configuration is absent or restored credentials
await review. Policy is checked before reading the DSN and again before each
event reaches the SDK transport.
"""

from __future__ import annotations

import os
from typing import Any

from sentry_sdk.transport import HttpTransport

from jarvis_common.maintenance import outbound_quarantine_active


class _QuarantineAwareSentryTransport(HttpTransport):
    """Block every Sentry envelope at the final HTTP transport boundary."""

    def _send_request(
        self,
        body: bytes,
        headers: dict[str, str],
        endpoint_type: Any,
        envelope: Any = None,
    ) -> None:
        if outbound_quarantine_active():
            return
        super()._send_request(body, headers, endpoint_type, envelope)


def maybe_init_sentry(service_name: str) -> None:
    """Initialize Sentry without default PII when outbound policy permits it.

    Parameters
    ----------
    service_name : str
        Non-secret service tag attached to emitted events.

    Notes
    -----
    If configuration is missing or post-restore outbound quarantine is active,
    the function returns without initializing Sentry. Events are checked again
    immediately before SDK transport handling, so a quarantine that begins
    after initialization still blocks telemetry.

    """
    if outbound_quarantine_active():
        return
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return
    import sentry_sdk  # noqa: PLC0415

    sentry_sdk.init(
        dsn=dsn,
        send_default_pii=False,
        traces_sample_rate=0.0,
        transport=_QuarantineAwareSentryTransport,
    )
    sentry_sdk.set_tag("service", service_name)
