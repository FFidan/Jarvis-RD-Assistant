"""Verify correlation_id_var propagates correctly across asyncio.create_task.

Risk-register item from the 2026-05-08 plan: Python 3.11+ documents that
``ContextVar`` values are copied into ``asyncio.create_task`` by default,
but our procrastinate task wrapper / SSE streamers / source plugins fan
work out via ``create_task`` — this test pins down the behavior we rely on.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from jarvis_common.logging_config import correlation_id_var


@pytest.mark.asyncio
async def test_correlation_id_propagates_into_create_task():
    """A child asyncio.Task sees the parent's correlation_id_var value."""
    parent_corr = uuid.uuid4()
    captured: list[uuid.UUID | None] = []

    async def child() -> None:
        captured.append(correlation_id_var.get())

    correlation_id_var.set(parent_corr)
    task = asyncio.create_task(child())
    await task

    assert captured == [parent_corr]


@pytest.mark.asyncio
async def test_correlation_id_isolated_per_task_when_child_overrides():
    """When child tasks call .set(), they don't leak into siblings or parent."""
    captured: dict[str, uuid.UUID | None] = {}

    async def child(name: str, set_to: uuid.UUID) -> None:
        correlation_id_var.set(set_to)
        # Tiny await to interleave with the sibling
        await asyncio.sleep(0)
        captured[name] = correlation_id_var.get()

    parent = uuid.uuid4()
    a = uuid.uuid4()
    b = uuid.uuid4()

    correlation_id_var.set(parent)
    await asyncio.gather(
        asyncio.create_task(child("a", a)),
        asyncio.create_task(child("b", b)),
    )

    assert captured["a"] == a
    assert captured["b"] == b
    # Parent context is unaffected by child .set() calls
    assert correlation_id_var.get() == parent


@pytest.mark.asyncio
async def test_correlation_id_propagates_through_nested_create_task():
    """Two levels deep: parent → child → grandchild all see the same id."""
    corr = uuid.uuid4()
    captured: list[uuid.UUID | None] = []

    async def grandchild() -> None:
        captured.append(correlation_id_var.get())

    async def child() -> None:
        captured.append(correlation_id_var.get())
        await asyncio.create_task(grandchild())

    correlation_id_var.set(corr)
    await asyncio.create_task(child())

    assert captured == [corr, corr]


@pytest.mark.asyncio
async def test_correlation_id_default_is_none_when_unset():
    """Fresh task with no parent .set() returns the ContextVar default."""
    # Reset any leakage from earlier tests by setting to None explicitly via
    # a new context (run inside a child task that the test framework owns).
    captured: list[uuid.UUID | None] = []

    async def child() -> None:
        captured.append(correlation_id_var.get())

    # Ensure parent's correlation_id_var is unset for this scope by running
    # the child inside a fresh asyncio.Task without first calling .set().
    # Because this test may run after others that .set() it, we explicitly
    # reset by setting to None.
    token = correlation_id_var.set(None)  # type: ignore[arg-type]
    try:
        await asyncio.create_task(child())
    finally:
        correlation_id_var.reset(token)

    assert captured == [None]
