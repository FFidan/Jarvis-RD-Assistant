"""Tests for the contract-docs link guard script."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_check_agent_docs():
    path = Path(__file__).resolve().parents[3] / "scripts" / "check_agent_docs.py"
    spec = importlib.util.spec_from_file_location("check_agent_docs_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_agent_docs = _load_check_agent_docs()


def test_iter_local_links_ignores_images_external_urls_and_mailto(tmp_path: Path) -> None:
    """Only ordinary Markdown local links should be returned for validation."""
    doc = tmp_path / "doc.md"
    doc.write_text(
        "\n".join(
            [
                "[local](docs/page.md)",
                "![image](img.png)",
                "[external](https://example.test)",
                "[mail](mailto:test@example.com)",
                "[anchored](docs/page.md#section)",
            ]
        ),
        encoding="utf-8",
    )

    assert check_agent_docs._iter_local_links(doc) == [
        (1, "docs/page.md"),
        (5, "docs/page.md"),
    ]


def test_link_exists_resolves_relative_to_source_file(tmp_path: Path) -> None:
    """Relative links are resolved from the document's directory."""
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "guide.md"
    target = docs / "target.md"
    source.write_text("[target](target.md)", encoding="utf-8")
    target.write_text("ok", encoding="utf-8")

    assert check_agent_docs._link_exists(source, "target.md")
    assert not check_agent_docs._link_exists(source, "missing.md")


def test_main_returns_zero_on_real_tree() -> None:
    """main() must pass cleanly against the actual repository tree."""
    assert check_agent_docs.main() == 0


def test_main_returns_one_when_link_broken(tmp_path: Path, monkeypatch) -> None:
    """main() must return 1 when a required contract doc has a broken local link."""
    for rel_path in check_agent_docs.PUBLIC_DOC_PATHS:
        stub = tmp_path / rel_path
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text("clean content\n", encoding="utf-8")

    # Inject a broken local link into one of the required docs.
    (tmp_path / check_agent_docs.PUBLIC_DOC_PATHS[0]).write_text(
        "[gone](./does-not-exist.md)\n", encoding="utf-8"
    )

    monkeypatch.setattr(check_agent_docs, "ROOT", tmp_path)
    assert check_agent_docs.main() == 1
