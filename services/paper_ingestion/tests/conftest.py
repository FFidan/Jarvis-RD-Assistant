"""Shared test fixtures for paper_ingestion tests.

Loaded automatically by pytest before any test file in this directory.
Module stubs MUST be at module level (not in fixtures) because they need
to be installed before any ``import app.*`` triggers transitive imports
of heavy dependencies that are only available inside Docker.
"""

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# 1. Path setup (replaces per-file sys.path.insert boilerplate)
# ---------------------------------------------------------------------------
_SERVICE_ROOT = str(Path(__file__).resolve().parents[1])
_JARVIS_COMMON = str(Path(__file__).resolve().parents[3] / "libs" / "jarvis_common")
for p in (_SERVICE_ROOT, _JARVIS_COMMON):
    if p not in sys.path:
        sys.path.insert(0, p)

# ---------------------------------------------------------------------------
# 2. Module stubs for Docker-only dependencies
#    Guards ensure existing per-file stubs are not overwritten.
# ---------------------------------------------------------------------------
if "fitz" not in sys.modules:
    sys.modules["fitz"] = MagicMock()

for _marker_mod in ("marker", "marker.converters", "marker.converters.pdf", "marker.models"):
    if _marker_mod not in sys.modules:
        sys.modules[_marker_mod] = MagicMock()

if "tiktoken" not in sys.modules:
    _fake_tiktoken = types.ModuleType("tiktoken")

    class _FakeEncoding:
        def encode(self, text):
            return list(text)

        def decode(self, tokens):
            return "".join(tokens)

    _fake_tiktoken.get_encoding = lambda _name: _FakeEncoding()
    sys.modules["tiktoken"] = _fake_tiktoken

if "qdrant_client" not in sys.modules:
    _fake_qdrant = types.ModuleType("qdrant_client")
    _fake_qdrant.AsyncQdrantClient = MagicMock()
    sys.modules["qdrant_client"] = _fake_qdrant

if "qdrant_client.models" not in sys.modules:
    from types import SimpleNamespace

    _fake_qm = types.ModuleType("qdrant_client.models")
    for _attr in (
        "Distance",
        "FieldCondition",
        "Filter",
        "MatchAny",
        "MatchValue",
        "PointIdsList",
        "PointStruct",
        "VectorParams",
        "RecommendInput",
        "RecommendQuery",
        "RecommendStrategy",
    ):
        setattr(_fake_qm, _attr, MagicMock())
    _fake_qm.Distance = SimpleNamespace(COSINE="cosine")
    _fake_qm.RecommendStrategy = SimpleNamespace(AVERAGE_VECTOR="average")
    sys.modules["qdrant_client.models"] = _fake_qm

if "rapidfuzz" not in sys.modules:
    _fake_rapidfuzz = types.ModuleType("rapidfuzz")
    _fake_rapidfuzz.fuzz = MagicMock()
    sys.modules["rapidfuzz"] = _fake_rapidfuzz

try:
    import python_multipart  # noqa: F401
except ImportError:
    for _mod in ("python_multipart", "multipart", "multipart.multipart"):
        if _mod not in sys.modules:
            sys.modules[_mod] = MagicMock()

# ---------------------------------------------------------------------------
# 3. FakeRecord + shared fixtures
# ---------------------------------------------------------------------------


class FakeRecord(dict):
    """Unified asyncpg.Record substitute: dict[], .attr, .keys(), .get(), .values()."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, key, default=None):
        return super().get(key, default)


def _make_pool_and_conn():
    """Create mock asyncpg Pool + Connection with transaction support."""
    conn = AsyncMock()
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


@pytest.fixture()
def mock_db():
    """Yield (pool, conn) tuple with mocked asyncpg pool."""
    return _make_pool_and_conn()


@pytest.fixture()
def fake_record():
    """Return the FakeRecord class for creating test records."""
    return FakeRecord
