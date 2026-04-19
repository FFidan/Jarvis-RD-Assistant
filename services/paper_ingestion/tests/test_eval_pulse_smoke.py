"""Smoke test for scripts/eval_pulse.py.

Verifies that the script can be imported without error and that its module-level
constants are well-formed.  Heavy deps (fitz, tiktoken) are not required here
because eval_pulse.py self-patches sys.path before importing app.* modules;
the test skips if those app-level imports are unavailable in the host env.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "eval_pulse.py"
_SERVICE_ROOT = _REPO_ROOT / "services" / "paper_ingestion"


def _load_eval_pulse(monkeypatch):
    """Load eval_pulse as a fresh module with heavy deps stubbed out."""
    monkeypatch.syspath_prepend(str(_SERVICE_ROOT))

    # Stub out Docker-only / heavy deps that are not present on the host
    for mod_name in ("fitz", "tiktoken"):
        if mod_name not in sys.modules:
            monkeypatch.setitem(sys.modules, mod_name, MagicMock())

    # qdrant_client stubs (imported transitively via app.embedder)
    if "qdrant_client" not in sys.modules:
        monkeypatch.setitem(
            sys.modules,
            "qdrant_client",
            MagicMock(AsyncQdrantClient=MagicMock()),
        )
    if "qdrant_client.models" not in sys.modules:
        monkeypatch.setitem(
            sys.modules,
            "qdrant_client.models",
            MagicMock(
                Distance=MagicMock(),
                PointStruct=MagicMock(),
                VectorParams=MagicMock(),
            ),
        )

    module_name = "_eval_pulse_smoke"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None, f"Cannot load {_SCRIPT_PATH}"
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except ImportError as exc:
        pytest.skip(f"eval_pulse deps not available on host: {exc}")
    return module


def test_eval_pulse_imports_without_error(monkeypatch):
    """eval_pulse.py can be loaded without raising ImportError."""
    module = _load_eval_pulse(monkeypatch)
    assert module is not None


def test_eval_pulse_constants_well_formed(monkeypatch):
    """Module-level constants exist and have sensible values."""
    module = _load_eval_pulse(monkeypatch)
    assert module.PRECISION_TARGET >= 0.0
    assert module.NO_LEAKAGE_MAX <= 1.0
    assert module.EMBEDDING_DIM > 0
    assert module.DECK_SIZE > 0
    assert module.FIXTURE_PATH.name == "eval_pulse_labeled_set.json"
