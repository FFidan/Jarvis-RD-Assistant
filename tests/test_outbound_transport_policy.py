"""Source-policy guard for outbound paths that must keep DNS pinning."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_every_sensitive_egress_path_uses_the_pinned_boundary() -> None:
    """New endpoint-specific clients cannot silently restore hostname connects."""
    app_factory = _source("libs/jarvis_common/jarvis_common/app_factory.py")
    paper_main = _source("services/paper_ingestion/paper_ingestion/main.py")
    provider_test = _source("services/paper_ingestion/paper_ingestion/services/provider_test.py")
    litellm_api = _source("services/paper_ingestion/paper_ingestion/services/litellm_api.py")
    model_assignment = _source(
        "services/paper_ingestion/paper_ingestion/services/model_assignment.py"
    )
    system_models = _source(
        "services/paper_ingestion/paper_ingestion/services/system_models_view.py"
    )
    zotero = _source("services/paper_ingestion/paper_ingestion/integrations/zotero_client.py")
    email = _source("libs/jarvis_common/jarvis_common/email.py")
    health = _source("libs/jarvis_common/jarvis_common/health.py")
    setup = _source("services/paper_ingestion/paper_ingestion/routers/setup.py")
    telegram = _source("services/telegram_bot/telegram_bot/main.py")
    launcher = _source("litellm/pinned_launcher.py")

    assert "PinnedAsyncTransport(JARVIS_SERVICE_POLICY)" in app_factory
    assert 'http_kwargs["trust_env"] = False' in app_factory
    assert "CachingTransport(PinnedAsyncTransport(JARVIS_SERVICE_POLICY))" in paper_main
    assert "pinned_async_client(" in provider_test
    assert "pinned_async_client(JARVIS_SERVICE_POLICY" in litellm_api
    assert "pinned_async_client(JARVIS_SERVICE_POLICY" in model_assignment
    assert "pinned_async_client(JARVIS_SERVICE_POLICY" in system_models
    assert "async with pinned_async_client(policy" in zotero
    assert "connect_pinned_socket(" in email
    assert "pinned_async_client(JARVIS_SERVICE_POLICY" in health
    assert "connect_pinned_socket(" in setup
    assert "pinned_async_client(" in telegram
    assert "PinnedAsyncTransport(LITELLM_PROVIDER_POLICY)" in launcher
    assert '"aclient_session"' in launcher
    assert '"disable_aiohttp_transport", True' in launcher

    # Security-sensitive production paths may not instantiate an ordinary
    # AsyncClient after their validation step.
    for source in (provider_test, zotero):
        assert "httpx.AsyncClient(" not in source


def test_production_async_client_constructors_are_exactly_the_guarded_factories() -> None:
    """A new direct HTTPX client is a reviewed egress boundary, never an accident."""
    roots = (
        ROOT / "libs" / "jarvis_common" / "jarvis_common",
        ROOT / "services",
        ROOT / "litellm",
    )
    allowed = {
        "libs/jarvis_common/jarvis_common/app_factory.py",
        "libs/jarvis_common/jarvis_common/pinned_transport.py",
        "libs/jarvis_common/jarvis_common/testing_contract_apps.py",
        "litellm/pinned_launcher.py",
    }
    found: set[str] = set()
    for root in roots:
        for path in root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            module_aliases = {
                imported.asname or imported.name
                for statement in ast.walk(tree)
                if isinstance(statement, ast.Import)
                for imported in statement.names
                if imported.name == "httpx"
            }
            class_aliases = {
                imported.asname or imported.name
                for statement in ast.walk(tree)
                if isinstance(statement, ast.ImportFrom) and statement.module == "httpx"
                for imported in statement.names
                if imported.name == "AsyncClient"
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                is_module_call = (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "AsyncClient"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in module_aliases
                )
                is_class_call = isinstance(node.func, ast.Name) and node.func.id in class_aliases
                if is_module_call or is_class_call:
                    found.add(str(path.relative_to(ROOT)))

    assert found == allowed
