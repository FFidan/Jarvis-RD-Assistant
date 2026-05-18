"""Characterization tests for xarch-001: task registry dispatch behavior.

These tests pin the observable dispatch contract BEFORE the refactor that
seals KIND_TO_TASK as an immutable mapping.  They must stay GREEN across
the refactor.  The contract under test:

  1. For every kind registered via register_tasks, the task object returned
     by the registry (KIND_TO_TASK[kind]) is the same object as the one
     stored in the procrastinate App.
  2. Registering the paper_ingestion handler set and the learning_engine
     handler set populates KIND_TO_TASK with exactly those kinds.
  3. KIND_TO_TASK is not writable after registration (post-refactor: sealed).
  4. A second isolated registration does not bleed into the first (no
     shared-mutable global side-effects).
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Minimal fake procrastinate App — no I/O, no connector
# ---------------------------------------------------------------------------


class _FakeApp:
    """Records @app.task registrations without touching a real connector."""

    def __init__(self) -> None:
        self.tasks: dict[str, object] = {}

    def task(self, *, name: str, queue: str, pass_context: bool):
        def _deco(fn):
            fn.queue = queue
            self.tasks[name] = fn
            return fn

        return _deco


# ---------------------------------------------------------------------------
# Shared dummy handlers (simulate the real handler callables)
# ---------------------------------------------------------------------------

_PAPER_KINDS = [
    "paper.process",
    "paper.analyze",
    "paper.summarize",
    "papers.batch_process",
    "papers.batch_summarize",
    "papers.scan_local",
    "citations.batch_fetch",
    "digest.weekly",
    "extraction.single",
    "extraction.batch",
    "contradictions.scan",
    "pulse.generate",
    "pulse.train_classifier",
    "model.pull",
    "zotero.push",
    "zotero.resync",
    "zotero.sync_from_zotero",
    "zotero.sync_annotations",
]

_LE_KINDS = [
    "card.generate",
    "card.generate_batch",
]


async def _dummy_handler(pool, http_client, payload, ctx):
    return {}


# ---------------------------------------------------------------------------
# Test 1: register_tasks populates the registry for all paper_ingestion kinds
# ---------------------------------------------------------------------------


def test_paper_ingestion_kinds_dispatch_identity() -> None:
    """All paper_ingestion kinds resolve to a task in the registry.

    Contract pinned: after calling register_tasks with the paper_ingestion
    kind set, every kind appears in both app.tasks and the returned/populated
    kind→task mapping with the same task object.
    """
    import jarvis_common.task_registry as tr

    fake_app = _FakeApp()
    mapping = {kind: _dummy_handler for kind in _PAPER_KINDS}

    # Use isolated TaskRegistry so we don't pollute the module-level singleton
    registry = tr.TaskRegistry(fake_app)  # type: ignore[arg-type]
    registry.register_tasks(mapping, queue="paper_ingestion")

    for kind in _PAPER_KINDS:
        assert kind in registry.kind_to_task, f"kind {kind!r} missing from kind_to_task"
        assert kind in fake_app.tasks, f"kind {kind!r} missing from app.tasks"
        assert registry.kind_to_task[kind] is fake_app.tasks[kind], (
            f"kind {kind!r}: kind_to_task object differs from app.tasks object"
        )


# ---------------------------------------------------------------------------
# Test 2: register_tasks populates the registry for all learning_engine kinds
# ---------------------------------------------------------------------------


def test_learning_engine_kinds_dispatch_identity() -> None:
    """All learning_engine kinds resolve to a task in the registry."""
    import jarvis_common.task_registry as tr

    fake_app = _FakeApp()
    mapping = {kind: _dummy_handler for kind in _LE_KINDS}

    registry = tr.TaskRegistry(fake_app)  # type: ignore[arg-type]
    registry.register_tasks(mapping, queue="learning_engine")

    for kind in _LE_KINDS:
        assert kind in registry.kind_to_task, f"kind {kind!r} missing from kind_to_task"
        assert kind in fake_app.tasks, f"kind {kind!r} missing from app.tasks"
        assert registry.kind_to_task[kind] is fake_app.tasks[kind], (
            f"kind {kind!r}: kind_to_task object differs from app.tasks object"
        )


# ---------------------------------------------------------------------------
# Test 3: module-level register_tasks populates the global KIND_TO_TASK
# ---------------------------------------------------------------------------


def test_module_level_register_populates_kind_to_task() -> None:
    """The module-level register_tasks writes into the public KIND_TO_TASK mapping.

    This pins the behavior that jobs_router dispatches against KIND_TO_TASK:
    after registration the kind must be retrievable from the public mapping.
    """
    import jarvis_common.task_registry as tr
    from jarvis_common.task_registry import register_tasks

    # We must pass the module-level app to hit the default-registry path
    kind = "_char_test.module_level"
    register_tasks(tr.app, mapping={kind: _dummy_handler}, queue="_char_q")  # type: ignore[arg-type]

    assert kind in tr.KIND_TO_TASK, (
        f"kind {kind!r} should be in KIND_TO_TASK after module-level register_tasks"
    )


# ---------------------------------------------------------------------------
# Test 4: two isolated registrations do not share state
# ---------------------------------------------------------------------------


def test_isolated_registrations_do_not_bleed() -> None:
    """Two independent TaskRegistry instances maintain separate kind→task maps."""
    import jarvis_common.task_registry as tr

    app_a = _FakeApp()
    app_b = _FakeApp()

    registry_a = tr.TaskRegistry(app_a)  # type: ignore[arg-type]
    registry_b = tr.TaskRegistry(app_b)  # type: ignore[arg-type]

    registry_a.register_tasks({"only.in_a": _dummy_handler}, queue="qa")
    registry_b.register_tasks({"only.in_b": _dummy_handler}, queue="qb")

    assert "only.in_a" in registry_a.kind_to_task
    assert "only.in_b" not in registry_a.kind_to_task

    assert "only.in_b" in registry_b.kind_to_task
    assert "only.in_a" not in registry_b.kind_to_task


# ---------------------------------------------------------------------------
# Test 5 (post-refactor gate): KIND_TO_TASK is sealed after registration
# ---------------------------------------------------------------------------


def test_kind_to_task_is_immutable_after_registration() -> None:
    """KIND_TO_TASK must not accept direct writes (MappingProxyType semantics).

    This test is RED on the pre-refactor code (KIND_TO_TASK is a plain dict).
    It turns GREEN after the xarch-001 refactor seals the mapping.

    The test is a forward-assertion: it documents the target invariant.
    Mark it xfail until the refactor lands, then remove the xfail mark.
    """
    import jarvis_common.task_registry as tr

    with pytest.raises(TypeError):
        tr.KIND_TO_TASK["_should_not_write"] = object()  # type: ignore[index]
