"""OpenAPI route-body guardrails for paper_ingestion."""

from __future__ import annotations

from typing import Any


def _openapi() -> dict[str, Any]:
    """Return the current paper_ingestion OpenAPI schema."""
    from paper_ingestion.main import app

    app.openapi_schema = None
    return app.openapi()


def _assert_request_body(schema: dict[str, Any], method: str, path: str) -> None:
    """Assert that an operation declares a JSON request body."""
    operation = schema["paths"][path][method]
    request_body = operation.get("requestBody")
    assert request_body, f"{method.upper()} {path} must declare requestBody"
    assert "application/json" in request_body.get("content", {}), (
        f"{method.upper()} {path} must accept application/json"
    )


def test_mutating_routes_do_not_expose_query_parameter_named_body() -> None:
    """Pydantic bodies must not regress into required query parameters named body."""
    schema = _openapi()
    offenders: list[str] = []
    for path, path_item in schema["paths"].items():
        for method in ("post", "put", "patch"):
            operation = path_item.get(method)
            if not operation:
                continue
            for parameter in operation.get("parameters", []):
                if parameter.get("in") == "query" and parameter.get("name") == "body":
                    offenders.append(f"{method.upper()} {path}")

    assert offenders == []


def test_frontend_critical_routes_declare_request_bodies() -> None:
    """Core UI workflows must keep JSON body contracts in OpenAPI."""
    schema = _openapi()
    for path in (
        "/api/search",
        "/api/search-preview",
        "/api/papers/search-hybrid",
        "/api/discover",
        "/api/analytics/fetch-and-process",
        "/api/contradictions/scan",
        "/api/papers/{paper_id}/contradictions/scan",
        "/api/pulse/rate",
        "/api/papers/{paper_id}/ask",
        "/api/papers/{paper_id}/ask/stream",
        "/api/ask",
        "/api/ask/stream",
    ):
        _assert_request_body(schema, "post", path)
