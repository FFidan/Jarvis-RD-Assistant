"""Guard: learning_engine task registration fails loudly (not a stripped assert)."""

import procrastinate
import pytest
from procrastinate.contrib.aiopg import AiopgConnector


def test_register_learning_engine_tasks_raises_when_kind_unregistered(monkeypatch):
    import jarvis_common.task_registry as tr
    import learning_engine._task_register as reg

    app = procrastinate.App(connector=AiopgConnector())
    # register_service_tasks delegates the real registration to register_tasks;
    # no-op it so the kinds never land in app.tasks and the missing-kind guard fires.
    monkeypatch.setattr(tr, "register_tasks", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="failed to register kinds"):
        reg.register_learning_engine_tasks(app)
