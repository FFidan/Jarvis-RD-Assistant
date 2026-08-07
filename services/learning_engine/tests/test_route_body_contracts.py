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


def test_intent_routes_declare_response_schemas() -> None:
    """GET/POST /api/executive/intent/today must expose the IntentRow response contract (M9)."""
    schema = _openapi()
    for method in ("get", "post"):
        operation = schema["paths"]["/api/executive/intent/today"][method]
        json_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert json_schema.get("$ref") == "#/components/schemas/IntentRow", (
            f"{method.upper()} /api/executive/intent/today must declare the IntentRow "
            f"response schema; got {json_schema!r}"
        )
    properties = schema["components"]["schemas"]["IntentRow"]["properties"]
    assert set(properties) == {"intent", "updated_at"}, (
        f"IntentRow response schema drifted: {sorted(properties)}"
    )


def test_my_day_bundle_declares_response_schema() -> None:
    """GET /api/executive/my-day-bundle must keep its MyDayBundleResponse contract (M9)."""
    schema = _openapi()
    operation = schema["paths"]["/api/executive/my-day-bundle"]["get"]
    json_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert json_schema.get("$ref") == "#/components/schemas/MyDayBundleResponse", (
        f"GET /api/executive/my-day-bundle must declare MyDayBundleResponse; got {json_schema!r}"
    )


def test_jobs_router_docstring_cites_only_registered_routes() -> None:
    """A route named in the jobs router's docstring must actually exist.

    The docstring pointed readers at ``/api/generation/batch`` while the batch
    endpoint is registered at ``/api/generate/batch``. A cross-reference naming
    no real route sends a maintainer hunting an endpoint that was never there,
    so every path the docstring cites is pinned against the live route table.
    """
    import re

    from learning_engine.routers import jobs

    schema = _openapi()
    cited = set(re.findall(r"/api/[a-zA-Z0-9/_-]+", jobs.__doc__ or ""))

    assert cited, "jobs router docstring cites no route — this guard needs updating"
    unknown = sorted(path for path in cited if path not in schema["paths"])
    assert not unknown, f"jobs router docstring cites unregistered route(s): {unknown}"
