from __future__ import annotations

import os


def maybe_init_sentry(service_name: str) -> None:
    """Initialize Sentry if SENTRY_DSN env var is set; otherwise a no-op."""
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return
    import sentry_sdk  # noqa: PLC0415

    sentry_sdk.init(
        dsn=dsn,
        send_default_pii=False,
        traces_sample_rate=0.0,
    )
    sentry_sdk.set_tag("service", service_name)
