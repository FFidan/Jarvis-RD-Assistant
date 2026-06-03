"""Contract test: first-boot model auto-configure (DB-backed).

Guards the W2-A fix: db/init.sql no longer pre-seeds the three
``llm.*_model`` rows with literal alias placeholders (``"smart"`` etc.).
Those placeholders used to block ``_autoconfigure_models_hook``'s
``INSERT ... ON CONFLICT DO NOTHING``, so the DB never received a real
model id and ``_rehydrate_litellm_aliases`` then pushed the bare alias
``"smart"`` to LiteLLM → ``ollama/smart`` 404.

This MUST be DB-backed (real asyncpg from init.sql): the placeholder
collision only manifests against the real ON CONFLICT semantics, which a
mocked connection cannot exercise.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis_common.testing import SharedConnPool
from paper_ingestion.main import _autoconfigure_models_hook
from paper_ingestion.services.model_lifecycle import HardwareInfo

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]


# The default Ollama pull set bootstrapped by setup.sh / litellm config on a
# 16 GB dev box. The autoconfigured smart model MUST be a member: a smart model
# that was never pulled would route to a 404. (OLLAMA_MODELS coherence invariant.)
_DEFAULT_PULLED_SET = {"qwen3:8b", "qwen3:4b", "qwen3-embedding:4b"}


async def _read_smart_model(conn):
    """Return the decoded llm.smart_model value (json codec → bare str) or None."""
    row = await conn.fetchrow(
        "SELECT value FROM user_config WHERE key = 'llm.smart_model' AND user_id IS NULL"
    )
    return None if row is None else row["value"]


async def test_first_boot_autoconfigure_seeds_real_pulled_model(contract_conn, monkeypatch):
    """First boot writes a real, pulled smart model and pushes it to LiteLLM.

    Verified anchors:
      - main.py:244 _autoconfigure_models_hook — detect tier, write best-fit
        models with INSERT ... ON CONFLICT DO NOTHING, then re-rehydrate.
      - main.py:214 _rehydrate_litellm_aliases — reads each llm.* row and pushes
        the stored model id to LiteLLM via update_litellm_model (lazy import).
    """
    pool = SharedConnPool(contract_conn)
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    # Deterministic tier-2 (MID, 16 GB) hardware so the recommendation is stable.
    monkeypatch.setattr(
        "paper_ingestion.services.model_lifecycle.detect_hardware",
        lambda: HardwareInfo(
            vram_gb=16.0,
            vram_source="nvidia-smi",
            tier=2,
            detected_at="2026-01-01T00:00:00Z",
            machine_id="test",
        ),
    )

    # Spy on the LiteLLM push so the test never hits a real proxy. Patched at the
    # source module because _rehydrate_litellm_aliases imports it lazily.
    pushed: dict[str, str] = {}

    async def _stub(key, model_id):  # noqa: ANN001
        pushed[key] = model_id

    monkeypatch.setattr("paper_ingestion.services.litellm_config.update_litellm_model", _stub)

    # PRE-ASSERT: the W2-A fix removed the placeholder seed, so the row is absent
    # on a fresh DB (this is what unblocks autoconfigure's ON CONFLICT INSERT).
    assert await _read_smart_model(contract_conn) is None

    # Settings read path tolerates the missing row: get_smart_model returns the
    # static alias, never touching the DB — no 500 on absence.
    from jarvis_common.db_helpers import get_smart_model

    assert get_smart_model() == "smart"

    await _autoconfigure_models_hook(app)

    # ASSERT: a real model id (not the alias "smart") is now persisted, and it is
    # a member of the default pulled set (OLLAMA_MODELS coherence invariant).
    smart = await _read_smart_model(contract_conn)
    assert smart is not None
    assert smart != "smart"
    assert smart in _DEFAULT_PULLED_SET, (
        f"autoconfigured smart model {smart!r} is not in the default pulled "
        f"set {_DEFAULT_PULLED_SET}"
    )

    # ASSERT (end-to-end): the post-autoconfigure rehydrate pushed the SAME real,
    # pulled model id to LiteLLM — proving the fix prevents the bare-alias 404.
    assert pushed.get("llm.smart_model") == smart
    assert pushed["llm.smart_model"] != "smart"

    # ASSERT: embed is NOT auto-configured (it is dimension-locked to the Qdrant
    # collection). The row stays absent so the LiteLLM `embed` alias keeps its
    # pulled static default (qwen3-embedding:4b); rehydrate never pushes it. This
    # prevents the tier recommender (e.g. mxbai-embed-large at 16 GB) from routing
    # embeddings to an unpulled, dimension-incompatible model.
    embed_row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = 'llm.embed_model' AND user_id IS NULL"
    )
    assert embed_row is None
    assert "llm.embed_model" not in pushed

    # Idempotency guard: a second hook call is a no-op (system.models_autoconfigured
    # short-circuits the write) and does not change the persisted value or raise.
    pushed.clear()
    await _autoconfigure_models_hook(app)
    assert await _read_smart_model(contract_conn) == smart
