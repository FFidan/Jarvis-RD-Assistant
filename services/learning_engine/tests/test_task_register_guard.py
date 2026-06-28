"""Guard: learning_engine task registration fails loudly (not a stripped assert)."""

import procrastinate
import pytest
from procrastinate.contrib.aiopg import AiopgConnector


def test_register_learning_engine_tasks_raises_when_kind_unregistered(monkeypatch):
    import learning_engine._task_register as reg

    app = procrastinate.App(connector=AiopgConnector())
    monkeypatch.setattr(reg, "register_tasks", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="failed to register kinds"):
        reg.register_learning_engine_tasks(app)
