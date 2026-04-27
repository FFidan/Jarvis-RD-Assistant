"""Test that extraction/__init__.py does not double-register job handlers.

H2 regression guard: importing paper_ingestion.extraction must not cause
_extraction_batch_job or _extraction_single_job to be registered a second
time in jarvis_common.jobs._HANDLERS.  The authoritative registration
happens when paper_ingestion.extraction.jobs is first imported; a re-export
via __init__ would trigger a duplicate import chain and register them twice.
"""

import importlib

import jarvis_common.jobs as _jobs_module


def test_extraction_init_does_not_double_register_handlers():
    """Import paper_ingestion.extraction twice and assert handler count is stable.

    Strategy:
    1. Ensure extraction.jobs has been imported (handlers registered once).
    2. Snapshot the handler keys for the two extraction kinds.
    3. Re-import paper_ingestion.extraction (simulates a second consumer doing
       ``from paper_ingestion.extraction import X``).
    4. Assert the same keys are still present and no duplicate registration
       occurred (len(_HANDLERS) unchanged and handlers are the same objects).
    """
    # Ensure the canonical registration has happened.
    import paper_ingestion.extraction.jobs  # noqa: F401

    extraction_kinds = ("extraction.single", "extraction.batch")

    # Both handlers must be registered after the first import.
    for kind in extraction_kinds:
        assert kind in _jobs_module._HANDLERS, (
            f"Handler '{kind}' missing from _HANDLERS after importing extraction.jobs"
        )

    # Capture the handler objects registered so far.
    handlers_before = {k: _jobs_module._HANDLERS[k] for k in extraction_kinds}
    total_before = len(_jobs_module._HANDLERS)

    # Re-import the extraction package (simulates a second import path that
    # previously triggered double-registration via __init__ re-exports).
    importlib.import_module("paper_ingestion.extraction")

    # Handler count must not grow.
    assert len(_jobs_module._HANDLERS) == total_before, (
        f"_HANDLERS grew from {total_before} to {len(_jobs_module._HANDLERS)} "
        "after re-importing paper_ingestion.extraction — double-registration detected"
    )

    # The registered handler objects must be identical (same function reference).
    for kind in extraction_kinds:
        assert _jobs_module._HANDLERS[kind] is handlers_before[kind], (
            f"Handler for '{kind}' was replaced after re-import — "
            "double-registration overwrote the original"
        )
