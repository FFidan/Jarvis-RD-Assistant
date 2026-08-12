"""Cross-service contracts that only a running stack can prove.

Every other cross-service test drives the apps in-process through an ASGI
transport, which bypasses DNS, the pinned outbound policy and the container
network entirely. These run against the live smoke stack over real HTTP, so a
refused or misrouted hop fails here instead of in a deployment.

``GET /health`` is deliberately minimal: it returns only ``{"status": ...}``
and answers 503 once any probe is unavailable. Per-dependency detail lives on
the authenticated ``GET /health/internal``, which is why the paper-ingestion
contract reads that route and asserts each dependency by name.
"""

import os

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.getenv("SMOKE_INTEGRATION") != "1", reason="integration gated")

# Dependencies that must report "ok" rather than merely "not unavailable".
# jarvis_common.health counts "unknown" as a passing probe, so a Qdrant probe
# that times out still leaves the overall /health status at "ok".
REQUIRED_PAPER_INGESTION_CHECKS = ("postgres", "qdrant", "litellm")

# Generous enough for a cold health sweep on a freshly booted stack.
HTTP_TIMEOUT_S = 30.0


def _required_env(name: str) -> str:
    value = os.getenv(name, "")
    assert value, f"{name} must be set to the running stack's address"
    return value


def test_paper_ingestion_reports_every_dependency_healthy() -> None:
    response = httpx.get(
        f"{_required_env('PAPER_INGESTION_BASE')}/health/internal",
        headers={"X-API-Key": _required_env("JARVIS_API_KEY")},
        timeout=HTTP_TIMEOUT_S,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["service"] == "paper_ingestion"
    checks = body["checks"]
    for dependency in REQUIRED_PAPER_INGESTION_CHECKS:
        assert checks.get(dependency) == "ok", f"{dependency} reported {checks.get(dependency)!r}"


def test_learning_engine_answers_over_real_http() -> None:
    response = httpx.get(
        f"{_required_env('LEARNING_ENGINE_BASE')}/health",
        timeout=HTTP_TIMEOUT_S,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
