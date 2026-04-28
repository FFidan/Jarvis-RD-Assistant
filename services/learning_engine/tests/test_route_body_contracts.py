"""OpenAPI route-body guardrails for learning_engine."""

from __future__ import annotations

from typing import Any


def _openapi() -> dict[str, Any]:
    """Return the current learning_engine OpenAPI schema."""
    from learning_engine.main import app

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


def test_generation_routes_declare_request_bodies() -> None:
    """Learning card generation endpoints must keep JSON body contracts."""
    schema = _openapi()
    for path in ("/api/generate", "/api/generate/batch"):
        _assert_request_body(schema, "post", path)
