"""Unit tests for the perf_probe module.

Tests verify:
  (a) disabled path: PERF_PROBE_ENABLED unset/0 → no file created, no I/O
  (b) enabled path: PERF_PROBE_ENABLED=1 → well-formed JSONL record appended
  (c) exception transparency: exception inside span re-raises, file not corrupted
  (d) import sanity: wired modules still import cleanly
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from datetime import datetime

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_perf_probe_module():
    """Restore sys.modules["paper_ingestion.perf_probe"] after each test.

    _reload_probe mutates sys.modules to force module-level constants to
    re-evaluate.  Without this teardown the reloaded (enabled, tmp-path)
    module instance leaks into the session and corrupts alphabetically-later
    test files (93 cascade failures).  We snapshot the original entry (or
    its absence) before the test body runs and unconditionally restore it
    afterward — monkeypatch already reverts the env vars, this covers the
    sys.modules side-effect.
    """
    _key = "paper_ingestion.perf_probe"
    _original = sys.modules.get(_key)
    yield
    if _original is None:
        sys.modules.pop(_key, None)
    else:
        sys.modules[_key] = _original


def _reload_probe(monkeypatch, *, enabled: bool, path: str | None = None):
    """Reload the probe module with the requested env flags applied.

    Returns the freshly-imported module so tests can call its API against
    the current env state without cross-test pollution.
    """
    monkeypatch.setenv("PERF_PROBE_ENABLED", "1" if enabled else "0")
    if path is not None:
        monkeypatch.setenv("PERF_PROBE_PATH", path)
    else:
        monkeypatch.delenv("PERF_PROBE_PATH", raising=False)

    # Force re-import so module-level constants are re-evaluated.
    if "paper_ingestion.perf_probe" in sys.modules:
        del sys.modules["paper_ingestion.perf_probe"]

    return importlib.import_module("paper_ingestion.perf_probe")


# ---------------------------------------------------------------------------
# (a) Disabled path — no I/O whatsoever
# ---------------------------------------------------------------------------


def test_disabled_no_file_created(tmp_path, monkeypatch):
    """probe_span does nothing when PERF_PROBE_ENABLED is not '1'."""
    probe_path = str(tmp_path / "perf" / "perf-probe.jsonl")
    probe = _reload_probe(monkeypatch, enabled=False, path=probe_path)

    with probe.probe_span("noop_op", extra="val"):
        pass  # no-op body

    assert not (tmp_path / "perf" / "perf-probe.jsonl").exists()


def test_disabled_zero_env(tmp_path, monkeypatch):
    """PERF_PROBE_ENABLED=0 is equivalent to unset."""
    probe_path = str(tmp_path / "perf-zero.jsonl")
    monkeypatch.setenv("PERF_PROBE_ENABLED", "0")
    monkeypatch.setenv("PERF_PROBE_PATH", probe_path)

    if "paper_ingestion.perf_probe" in sys.modules:
        del sys.modules["paper_ingestion.perf_probe"]
    probe = importlib.import_module("paper_ingestion.perf_probe")

    with probe.probe_span("zero_op"):
        time.sleep(0)  # intentionally tiny

    assert not os.path.exists(probe_path)


def test_disabled_unset_env(tmp_path, monkeypatch):
    """PERF_PROBE_ENABLED absent is equivalent to '0'."""
    probe_path = str(tmp_path / "perf-unset.jsonl")
    monkeypatch.delenv("PERF_PROBE_ENABLED", raising=False)
    monkeypatch.setenv("PERF_PROBE_PATH", probe_path)

    if "paper_ingestion.perf_probe" in sys.modules:
        del sys.modules["paper_ingestion.perf_probe"]
    probe = importlib.import_module("paper_ingestion.perf_probe")

    with probe.probe_span("unset_op"):
        pass

    assert not os.path.exists(probe_path)


# ---------------------------------------------------------------------------
# (b) Enabled path — well-formed JSONL record
# ---------------------------------------------------------------------------


def test_enabled_appends_jsonl_record(tmp_path, monkeypatch):
    """probe_span appends exactly one well-formed JSONL record when enabled."""
    probe_path = str(tmp_path / "sub" / "perf.jsonl")
    probe = _reload_probe(monkeypatch, enabled=True, path=probe_path)

    with probe.probe_span("my_span", n=3, model="fast"):
        pass

    assert os.path.exists(probe_path), "JSONL file must be created when enabled"
    lines = [ln.strip() for ln in open(probe_path).readlines() if ln.strip()]
    assert len(lines) == 1, f"Expected exactly 1 line, got {len(lines)}"

    record = json.loads(lines[0])
    assert record["span"] == "my_span"
    assert isinstance(record["ms"], float)
    assert record["ms"] >= 0.0
    assert "ts" in record  # ISO UTC timestamp
    assert record["n"] == 3
    assert record["model"] == "fast"


def test_enabled_appends_multiple_records(tmp_path, monkeypatch):
    """Multiple probe_span calls append multiple records to the same file."""
    probe_path = str(tmp_path / "multi.jsonl")
    probe = _reload_probe(monkeypatch, enabled=True, path=probe_path)

    for i in range(3):
        with probe.probe_span(f"op_{i}", idx=i):
            pass

    lines = [ln.strip() for ln in open(probe_path).readlines() if ln.strip()]
    assert len(lines) == 3
    for i, line in enumerate(lines):
        rec = json.loads(line)
        assert rec["span"] == f"op_{i}"
        assert rec["idx"] == i


def test_enabled_ms_measures_elapsed(tmp_path, monkeypatch):
    """probe_span ms field reflects a non-trivial sleep duration."""
    probe_path = str(tmp_path / "timing.jsonl")
    probe = _reload_probe(monkeypatch, enabled=True, path=probe_path)

    with probe.probe_span("timed_op"):
        time.sleep(0.05)  # 50 ms

    record = json.loads(open(probe_path).read().strip())
    # Allow generous range: 30ms–5000ms to avoid flaky CI
    assert record["ms"] >= 30.0, f"ms={record['ms']} less than expected minimum"


def test_enabled_ts_is_iso_utc(tmp_path, monkeypatch):
    """ts field is a parseable ISO 8601 UTC timestamp."""
    probe_path = str(tmp_path / "ts.jsonl")
    probe = _reload_probe(monkeypatch, enabled=True, path=probe_path)

    with probe.probe_span("ts_test"):
        pass

    record = json.loads(open(probe_path).read().strip())
    # Should parse without error
    dt = datetime.fromisoformat(record["ts"].replace("Z", "+00:00"))
    assert dt.tzinfo is not None


def test_enabled_lazy_dir_creation(tmp_path, monkeypatch):
    """Parent directories are created lazily on first write."""
    deeply_nested = str(tmp_path / "a" / "b" / "c" / "probe.jsonl")
    probe = _reload_probe(monkeypatch, enabled=True, path=deeply_nested)

    with probe.probe_span("nested"):
        pass

    assert os.path.exists(deeply_nested)


# ---------------------------------------------------------------------------
# (c) Exception transparency — span is recorded, exception re-raised
# ---------------------------------------------------------------------------


def test_exception_inside_span_reraises(tmp_path, monkeypatch):
    """Exception inside the span body is re-raised unchanged."""
    probe_path = str(tmp_path / "exc.jsonl")
    probe = _reload_probe(monkeypatch, enabled=True, path=probe_path)

    class _SentinelError(Exception):
        pass

    with pytest.raises(_SentinelError):
        with probe.probe_span("exc_op", tag="sentinel"):
            raise _SentinelError("test-exception")


def test_exception_inside_span_still_records(tmp_path, monkeypatch):
    """A span that exits via exception still appends a record."""
    probe_path = str(tmp_path / "exc_record.jsonl")
    probe = _reload_probe(monkeypatch, enabled=True, path=probe_path)

    try:
        with probe.probe_span("exc_record_op"):
            raise ValueError("boom")
    except ValueError:
        pass

    assert os.path.exists(probe_path), "record must be written even on exception"
    rec = json.loads(open(probe_path).read().strip())
    assert rec["span"] == "exc_record_op"
    assert isinstance(rec["ms"], float)


def test_exception_inside_disabled_span_still_reraises(monkeypatch):
    """Disabled probe is transparent for exception re-raise too."""
    probe = _reload_probe(monkeypatch, enabled=False)

    class _BoomError(RuntimeError):
        pass

    with pytest.raises(_BoomError):
        with probe.probe_span("noop_exc"):
            raise _BoomError("disabled-reraise")


# ---------------------------------------------------------------------------
# (d) Import sanity for wired modules
# ---------------------------------------------------------------------------


def test_scoring_module_imports(monkeypatch):
    """pulse/scoring.py still imports cleanly after wiring."""
    monkeypatch.delenv("PERF_PROBE_ENABLED", raising=False)

    # A broken wiring import would cause collection to fail for the whole
    # suite; a plain import (no sys.modules mutation) is sufficient to
    # assert the contract without polluting the session.
    import paper_ingestion.pulse.scoring  # noqa: F401


def test_embedder_module_imports(monkeypatch):
    """ingestion/embedder.py still imports cleanly after wiring."""
    monkeypatch.delenv("PERF_PROBE_ENABLED", raising=False)

    import paper_ingestion.ingestion.embedder  # noqa: F401


def test_streaming_module_imports(monkeypatch):
    """rag/streaming.py still imports cleanly after wiring."""
    monkeypatch.delenv("PERF_PROBE_ENABLED", raising=False)

    import paper_ingestion.rag.streaming  # noqa: F401
