"""Tests for optional Pulse citation graph signals."""

from __future__ import annotations

import builtins
import sys
from types import ModuleType

import pytest
from paper_ingestion.pulse.citation_signals import compute_citation_signals
from tests.conftest import FakeRecord, _make_pool_and_conn


class FakeGraph:
    def __init__(self) -> None:
        self._nodes: set[int] = set()
        self._edges: set[tuple[int, int]] = set()

    def add_node(self, node: int) -> None:
        self._nodes.add(node)

    def add_edge(self, source: int, target: int) -> None:
        self._nodes.update({source, target})
        self._edges.add(tuple(sorted((source, target))))

    def number_of_nodes(self) -> int:
        return len(self._nodes)

    def number_of_edges(self) -> int:
        return len(self._edges)

    @property
    def nodes(self) -> set[int]:
        return self._nodes


def _block_networkx_import(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: ANN001
        if name == "networkx" or name.startswith("networkx."):
            raise ImportError("networkx intentionally unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def _install_fake_networkx(monkeypatch: pytest.MonkeyPatch) -> None:
    networkx_mod = ModuleType("networkx")
    networkx_mod.Graph = FakeGraph  # type: ignore[attr-defined]
    networkx_mod.pagerank = lambda _graph: {1: 2.0, 2: 1.0, 3: 0.5}  # type: ignore[attr-defined]
    networkx_mod.adamic_adar_index = lambda _graph, ebunch: (  # type: ignore[attr-defined]
        (source, target, 0.4) for source, target in ebunch
    )
    monkeypatch.setitem(sys.modules, "networkx", networkx_mod)


@pytest.mark.asyncio
async def test_compute_citation_signals_empty_input_does_not_touch_db():
    pool, _conn = _make_pool_and_conn()

    result = await compute_citation_signals(pool, [])

    assert result == {}
    pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_compute_citation_signals_missing_networkx_falls_back(
    monkeypatch: pytest.MonkeyPatch,
):
    _block_networkx_import(monkeypatch)
    pool, _conn = _make_pool_and_conn()

    result = await compute_citation_signals(pool, ["arxiv:1"])

    assert result == {}
    pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_compute_citation_signals_normalizes_small_graph(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_fake_networkx(monkeypatch)
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [
        FakeRecord(
            {
                "external_id": "arxiv:1",
                "id": 1,
                "citation_count": 10,
                "is_candidate": True,
                "is_liked": False,
                "source_paper_id": 1,
                "cited_paper_id": 2,
            }
        ),
        FakeRecord(
            {
                "external_id": "arxiv:2",
                "id": 2,
                "citation_count": 5,
                "is_candidate": True,
                "is_liked": True,
                "source_paper_id": 2,
                "cited_paper_id": 3,
            }
        ),
        FakeRecord(
            {
                "external_id": "arxiv:3",
                "id": 3,
                "citation_count": 0,
                "is_candidate": True,
                "is_liked": False,
                "source_paper_id": None,
                "cited_paper_id": None,
            }
        ),
    ]

    result = await compute_citation_signals(pool, ["arxiv:1", "arxiv:2", "arxiv:3"])

    conn.fetch.assert_awaited_once()
    assert result == {
        "arxiv:1": {
            "citation_pagerank": 1.0,
            "citation_count": 1.0,
            "citation_adamic_adar": 1.0,
        },
        "arxiv:2": {
            "citation_pagerank": 0.5,
            "citation_count": 0.5,
            "citation_adamic_adar": 0.0,
        },
        "arxiv:3": {
            "citation_pagerank": 0.25,
            "citation_count": 0.0,
            "citation_adamic_adar": 1.0,
        },
    }
