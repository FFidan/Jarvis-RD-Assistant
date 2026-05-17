"""Performance probe — flag-gated hot-path timing helper.

OFF BY DEFAULT — production path is byte-unchanged.
-----------------------------------------------------
Enable with: ``PERF_PROBE_ENABLED=1``
Set output path with: ``PERF_PROBE_PATH=/path/to/probe.jsonl``
  (default: ``artifacts/perf/perf-probe.jsonl``)

When disabled (the default): ``probe_span`` is a zero-cost context manager
that does nothing — no ``time.perf_counter``, no file open, no JSON
serialization.  The production code path is identical to code without the
probe wrapper.

When enabled: each ``with probe_span(name, **fields):`` block appends one
newline-delimited JSON record to the output file::

    {"span": "embed_texts_post", "ms": 42.1, "ts": "2026-05-17T12:00:00.123456+00:00", "n_texts": 8}

The output directory is created lazily on first write.

Thread/asyncio safety: ``open(path, "a")`` + one write per ``__exit__``.
Python's file-append in asyncio is safe for single-process use.  File
flushing is explicit to avoid partial records.

Dependencies: stdlib only (``os``, ``time``, ``json``, ``contextlib``,
``datetime``, ``pathlib``).
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Named constants — referenced by wired call sites so they appear in tracebacks
# ---------------------------------------------------------------------------

#: Enable flag — set to "1" to turn on timing probes.  Any other value
#: (including absent) leaves the probe in the no-op fast path.
PERF_PROBE_ENV_ENABLED = "PERF_PROBE_ENABLED"

#: Path env — absolute or relative path for the JSONL output file.
PERF_PROBE_ENV_PATH = "PERF_PROBE_PATH"

#: Default output path (relative to CWD) when PERF_PROBE_PATH is unset.
PERF_PROBE_DEFAULT_PATH = "artifacts/perf/perf-probe.jsonl"

# ---------------------------------------------------------------------------
# Module-level state — evaluated once at import time
# ---------------------------------------------------------------------------

_ENABLED: bool = os.environ.get(PERF_PROBE_ENV_ENABLED, "0").strip() == "1"
_PROBE_PATH: str = os.environ.get(PERF_PROBE_ENV_PATH, PERF_PROBE_DEFAULT_PATH).strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@contextmanager
def probe_span(name: str, **fields):
    """Context manager that records a timing span when the probe is enabled.

    Parameters
    ----------
    name:
        Logical name for this span (e.g. ``"embed_texts_post"``).
    **fields:
        Arbitrary key-value pairs included verbatim in the JSON record.

    When ``PERF_PROBE_ENABLED`` is not ``"1"`` this function is a true
    no-op: no ``time.perf_counter`` call, no file access, no JSON work.
    The production runtime overhead is a single attribute lookup plus the
    context-manager protocol enter/exit (effectively zero).

    On exit (normal or exception) the span is recorded.  The exception,
    if any, is re-raised unchanged so the surrounding code is transparent.
    """
    if not _ENABLED:
        # ---- DISABLED path: absolute no-op --------------------------------
        yield
        return

    # ---- ENABLED path: record entry time, yield, record exit time ---------
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - t0) * 1_000.0
        ts = datetime.now(UTC).isoformat()
        record: dict = {"span": name, "ms": elapsed_ms, "ts": ts, **fields}
        _append_record(record)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _append_record(record: dict) -> None:
    """Append a single JSONL record to the probe output file.

    Creates parent directories lazily on first call.
    """
    path = Path(_PROBE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
