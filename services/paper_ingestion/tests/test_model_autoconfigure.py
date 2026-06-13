"""First-boot model auto-configure: chooser units + DB-backed contract tests.

Guards two generations of fixes:

* db/init.sql no longer pre-seeds the three ``llm.*_model`` rows with
  literal alias placeholders (``"smart"`` etc.), which used to block
  ``_autoconfigure_models_hook``'s ``INSERT ... ON CONFLICT DO NOTHING`` and
  let the boot delivery push the bare alias ``"smart"`` to LiteLLM →
  ``ollama/smart`` 404.
* The hook fetches the real Ollama ``/api/tags`` list and picks the
  largest installed model that fits beside the embedder
  (``_choose_autoconfigured_model``), seeding a safe per-machine num_ctx row
  alongside each role. The old behavior (``installed=[]`` → smallest-first)
  survives only as the CPU / tags-fetch-failure carve-out.

Delivery itself is owned by the boot reconciler
(``_reconcile_litellm_models_once``), which runs right after the hook in
the lifespan — the main contract test drives one reconcile pass to prove the
persisted choice is what reaches LiteLLM.

The DB-backed tests MUST stay contract-shaped (real asyncpg from init.sql):
the placeholder collision and the never-clobber guarantee only manifest
against real ON CONFLICT semantics. The chooser tests are pure-function units
driven by fixture tag lists — never the shared ai-infra daemon's tags.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from jarvis_common.testing import SharedConnPool
from paper_ingestion.ingestion.embedding_config import EMBEDDING_MODEL_NAME
from paper_ingestion.main import (
    _autoconfigure_models_hook,
    _choose_autoconfigured_model,
    _fetch_installed_ollama_models,
    _reconcile_litellm_models_once,
)
from paper_ingestion.services.model_lifecycle import (
    NUM_CTX_LADDER,
    HardwareInfo,
    catalog_entry_for_model,
    recommendations_for_role,
    safe_num_ctx,
)

# The default Ollama pull set bootstrapped by setup.sh / litellm config on a
# 16 GB dev box. The autoconfigured smart model MUST be a member: a smart model
# that was never pulled would route to a 404. (OLLAMA_MODELS coherence invariant.)
_DEFAULT_PULLED_SET = {"qwen3:8b", "qwen3:4b", "qwen3-embedding:4b"}


# ---------------------------------------------------------------------------
# Shared fixtures/helpers
# ---------------------------------------------------------------------------


def _installed(*tags: str) -> list[dict[str, Any]]:
    return [{"name": tag, "size": 1, "details": {}} for tag in tags]


def _hw(vram_gb: float, tier: int) -> HardwareInfo:
    return HardwareInfo(
        vram_gb=vram_gb,
        vram_source="nvidia-smi" if vram_gb > 0.0 else "cpu",
        tier=tier,
        detected_at="2026-01-01T00:00:00Z",
        machine_id="test",
    )


def _hook_embed_reserve_gb() -> float:
    """Same embed reserve the hook derives: catalog vram_gb of the static embedder."""
    entry = catalog_entry_for_model(EMBEDDING_MODEL_NAME)
    return entry.vram_gb if entry is not None else 0.0


def _choose(role: str, tags: tuple[str, ...], hw: HardwareInfo) -> Mapping[str, Any] | None:
    recs = recommendations_for_role(
        role,  # type: ignore[arg-type]
        installed=_installed(*tags),
        current={},
        embedding_model_name="",
        hardware=hw,
        cloud_api_keys={},
    )
    return _choose_autoconfigured_model(recs, hw, _hook_embed_reserve_gb())


class _TagsClient:
    """Boundary fake for the Ollama /api/tags probe."""

    def __init__(self, tags: tuple[str, ...] | None = None, exc: Exception | None = None):
        self._tags = tags or ()
        self._exc = exc

    async def get(self, url: str, **kwargs: Any) -> Any:
        if self._exc is not None:
            raise self._exc
        payload = {"models": _installed(*self._tags)}
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: payload)


# ---------------------------------------------------------------------------
# Chooser units (pure; fixture-driven tag lists)
# ---------------------------------------------------------------------------


def test_chooser_tier_tie_prefers_8b_for_smart() -> None:
    """qwen3:4b and qwen3:8b are BOTH catalog tier 1 — vram DESC is what prefers 8b."""
    best = _choose("smart", ("qwen3:4b", "qwen3:8b", "qwen3-embedding:4b"), _hw(16.0, 2))
    assert best is not None and best["id"] == "qwen3:8b"


def test_chooser_fast_role_picks_4b() -> None:
    """qwen3:4b is the only installed catalog entry carrying the fast role."""
    best = _choose("fast", ("qwen3:4b", "qwen3:8b", "qwen3-embedding:4b"), _hw(16.0, 2))
    assert best is not None and best["id"] == "qwen3:4b"


def test_chooser_embed_reserve_rejects_14b_on_16gb() -> None:
    """qwen3:14b alone fits a 16 GB card (10.0 ≤ 13.6 GB single-model plane) but
    NOT beside the 3.0 GB always-resident embedder under the 80% co-residency
    budget (10.0 + 3.0 = 13.0 > 12.8) — the reserve is what rejects it."""
    best = _choose(
        "smart", ("qwen3:4b", "qwen3:8b", "qwen3:14b", "qwen3-embedding:4b"), _hw(16.0, 2)
    )
    assert best is not None and best["id"] == "qwen3:8b"


def test_chooser_single_installed_model() -> None:
    best = _choose("smart", ("qwen3:4b",), _hw(16.0, 2))
    assert best is not None and best["id"] == "qwen3:4b"


def test_chooser_cpu_carveout_keeps_smallest_first() -> None:
    """vram_gb == 0.0 → today's smallest-first pick. qwen3:4b is the smallest
    catalog LLM (qwen3:1.7b is NOT a model_catalog.json entry)."""
    best = _choose("smart", ("qwen3:4b", "qwen3:8b"), _hw(0.0, 0))
    assert best is not None and best["id"] == "qwen3:4b"


def test_chooser_empty_installed_falls_back_smallest_first() -> None:
    """installed=[] (tags fetch failed) → today's catalog-only smallest-first pick."""
    best = _choose("smart", (), _hw(16.0, 2))
    assert best is not None and best["id"] == "qwen3:4b"


# ---------------------------------------------------------------------------
# /api/tags boundary adapter
# ---------------------------------------------------------------------------


async def test_fetch_installed_models_returns_tag_list() -> None:
    app = SimpleNamespace(state=SimpleNamespace(http_client=_TagsClient(tags=("qwen3:8b",))))
    assert await _fetch_installed_ollama_models(app) == _installed("qwen3:8b")  # type: ignore[arg-type]


async def test_fetch_installed_models_failure_returns_empty() -> None:
    """An unreachable Ollama must not crash boot — the hook falls back to []."""
    app = SimpleNamespace(
        state=SimpleNamespace(http_client=_TagsClient(exc=httpx.ConnectError("boom")))
    )
    assert await _fetch_installed_ollama_models(app) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DB-backed contract tests (real asyncpg; skip without JARVIS_RUN_LIVE_PG=1)
# ---------------------------------------------------------------------------


async def _read_config_value(conn: Any, key: str) -> Any:
    """Return the decoded NULL-user config value for *key* (json codec) or None."""
    row = await conn.fetchrow(
        "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL", key
    )
    return None if row is None else row["value"]


def _patch_hardware(monkeypatch: pytest.MonkeyPatch, hw: HardwareInfo) -> None:
    # The hook binds detect_hardware at import time — patch the consuming namespace.
    monkeypatch.setattr("paper_ingestion.main.detect_hardware", lambda: hw)


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_first_boot_autoconfigure_seeds_real_pulled_model(contract_conn, monkeypatch):
    """First boot writes the largest fitting pulled model + a safe num_ctx row;
    the reconciler delivers the persisted model.

    Verified anchors:
      - main.py _autoconfigure_models_hook — fetch /api/tags, choose per role,
        write model + per-machine num_ctx with INSERT ... ON CONFLICT DO NOTHING.
      - main.py _reconcile_litellm_models_once — reads each llm.* row and
        delivers the stored model id to LiteLLM via update_litellm_model
        (which itself resolves llm.<machine>.<role>_num_ctx via _get_num_ctx).
    """
    pool = SharedConnPool(contract_conn)
    hw = _hw(16.0, 2)  # deterministic tier-2 (MID, 16 GB), machine_id="test"
    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            http_client=_TagsClient(tags=tuple(_DEFAULT_PULLED_SET)),
        )
    )
    _patch_hardware(monkeypatch, hw)

    # Spy on the LiteLLM delivery so the test never hits a real proxy. Patched
    # at the source module because the reconciler imports it lazily.
    pushed: dict[str, str] = {}

    async def _stub(key, model_id, **kwargs):  # noqa: ANN001, ANN003
        pushed[key] = model_id
        return True

    monkeypatch.setattr("paper_ingestion.services.litellm_config.update_litellm_model", _stub)

    async def _fallback_stub(fast_model, **kwargs):  # noqa: ANN001, ANN003
        pushed["smart-fallback"] = fast_model
        return True

    monkeypatch.setattr(
        "paper_ingestion.services.litellm_config.ensure_smart_fallback", _fallback_stub
    )

    # PRE-ASSERT: init.sql no longer seeds placeholder rows, so the row is absent
    # on a fresh DB (this is what unblocks autoconfigure's ON CONFLICT INSERT).
    assert await _read_config_value(contract_conn, "llm.smart_model") is None

    # Settings read path tolerates the missing row: get_smart_model returns the
    # static alias, never touching the DB — no 500 on absence.
    from jarvis_common.db_helpers import get_smart_model

    assert get_smart_model() == "smart"

    await _autoconfigure_models_hook(app)

    # ASSERT: with {4b, 8b, embed} installed on 16 GB, the chooser takes the
    # largest fitting LLM per role (tier tie → vram DESC): smart=8b, fast=4b.
    smart = await _read_config_value(contract_conn, "llm.smart_model")
    assert smart == "qwen3:8b"
    assert smart in _DEFAULT_PULLED_SET  # OLLAMA_MODELS coherence invariant
    assert await _read_config_value(contract_conn, "llm.fast_model") == "qwen3:4b"

    # ASSERT (D9): a safe per-machine num_ctx row is seeded WITH each model and
    # matches the safe_num_ctx derivation (exact stop values are pinned by the
    # pure tests in test_model_lifecycle.py).
    reserve = _hook_embed_reserve_gb()
    smart_ctx = await _read_config_value(contract_conn, "llm.test.smart_num_ctx")
    fast_ctx = await _read_config_value(contract_conn, "llm.test.fast_num_ctx")
    assert smart_ctx == safe_num_ctx(catalog_entry_for_model("qwen3:8b"), hw, reserve)
    assert fast_ctx == safe_num_ctx(catalog_entry_for_model("qwen3:4b"), hw, reserve)
    assert smart_ctx in NUM_CTX_LADDER and fast_ctx in NUM_CTX_LADDER

    # ASSERT (end-to-end): the boot reconciler pass (run by the lifespan right
    # after the hook) delivers the SAME real, pulled model id to LiteLLM —
    # proving the fix prevents the bare-alias 404.
    assert await _reconcile_litellm_models_once(pool) is True
    assert pushed.get("llm.smart_model") == smart
    assert pushed["llm.smart_model"] != "smart"

    # ASSERT: embed is NOT auto-configured (it is dimension-locked to the Qdrant
    # collection). The row stays absent so the LiteLLM `embed` alias keeps its
    # YAML-seeded static default (qwen3-embedding:4b); the reconciler never
    # delivers it. This prevents the tier recommender (e.g. mxbai-embed-large at
    # 16 GB) from routing embeddings to an unpulled, dimension-incompatible model.
    assert await _read_config_value(contract_conn, "llm.embed_model") is None
    assert "llm.embed_model" not in pushed

    # Idempotency guard: a second hook call is a no-op (system.models_autoconfigured
    # short-circuits the write) and does not change the persisted values or raise.
    pushed.clear()
    await _autoconfigure_models_hook(app)
    assert await _read_config_value(contract_conn, "llm.smart_model") == smart
    assert await _read_config_value(contract_conn, "llm.test.smart_num_ctx") == smart_ctx


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_autoconfigure_tags_fetch_failure_keeps_legacy_fallback(contract_conn, monkeypatch):
    """Ollama unreachable at hook time → installed=[] → today's smallest-first
    seeding (qwen3:4b for smart) instead of crashing boot."""
    pool = SharedConnPool(contract_conn)
    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            http_client=_TagsClient(exc=httpx.ConnectError("ollama down")),
        )
    )
    _patch_hardware(monkeypatch, _hw(16.0, 2))

    await _autoconfigure_models_hook(app)

    assert await _read_config_value(contract_conn, "llm.smart_model") == "qwen3:4b"
    assert await _read_config_value(contract_conn, "system.models_autoconfigured") is True


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_autoconfigure_never_clobbers_manual_num_ctx(contract_conn, monkeypatch):
    """A pre-existing per-machine num_ctx row (manual operator choice) survives
    the hook untouched — ON CONFLICT DO NOTHING semantics, same as the model rows."""
    pool = SharedConnPool(contract_conn)
    await contract_conn.execute(
        "INSERT INTO user_config (key, value) VALUES ($1, $2::jsonb)",
        "llm.test.smart_num_ctx",
        4096,
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            http_client=_TagsClient(tags=tuple(_DEFAULT_PULLED_SET)),
        )
    )
    _patch_hardware(monkeypatch, _hw(16.0, 2))

    await _autoconfigure_models_hook(app)

    assert await _read_config_value(contract_conn, "llm.test.smart_num_ctx") == 4096
    # The other role's row is still seeded fresh alongside its model.
    assert await _read_config_value(contract_conn, "llm.fast_model") == "qwen3:4b"
    assert await _read_config_value(contract_conn, "llm.test.fast_num_ctx") in NUM_CTX_LADDER
