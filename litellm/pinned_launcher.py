"""Start the pinned LiteLLM image with guarded custom-provider egress.

This runs in the same Python process as LiteLLM.  Executing the ``litellm``
console script after assigning ``aclient_session`` would discard the hook.
"""

from __future__ import annotations

import sys


def main() -> None:
    """Validate the reviewed image contract, install the client, and serve."""
    import importlib

    import httpcore
    import httpx
    from jarvis_common.pinned_transport import LITELLM_PROVIDER_POLICY, PinnedAsyncTransport
    from litellm.proxy.proxy_cli import run_server

    import litellm

    expected = ("1.84.0", "0.28.1", "1.0.9")
    actual = (
        importlib.import_module("litellm._version").version,
        httpx.__version__,
        httpcore.__version__,
    )
    if actual != expected:
        raise RuntimeError(f"Unsupported LiteLLM transport contract: {actual!r}")
    if not hasattr(litellm, "aclient_session") or not hasattr(litellm, "disable_aiohttp_transport"):
        raise RuntimeError("LiteLLM custom-provider transport hook is unavailable")

    # The custom OpenAI-compatible handler must use the guarded HTTPX session;
    # aiohttp would resolve/connect outside the pinning boundary.
    setattr(litellm, "disable_aiohttp_transport", True)
    setattr(
        litellm,
        "aclient_session",
        httpx.AsyncClient(
            transport=PinnedAsyncTransport(LITELLM_PROVIDER_POLICY),
            trust_env=False,
        ),
    )
    run_server.main(args=sys.argv[1:], prog_name="litellm")


if __name__ == "__main__":
    main()
