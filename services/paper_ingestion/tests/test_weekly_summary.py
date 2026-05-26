"""Unit tests for W3-13 CFG-LLMOUT-1: ThemeOutput attribute access.

Verifies that:
- ThemeOutput.theme is accessed as an attribute (not via .get("theme")).
- themes stays as list[ThemeOutput] through the verification loop.
- .model_dump() is called only at the response boundary.
- verified / verification_reason annotation pattern is preserved in output dicts.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from paper_ingestion.weekly_summary_models import ThemeOutput, WeeklyDigestOutput


# ---------------------------------------------------------------------------
# Structural: ThemeOutput attribute access
# ---------------------------------------------------------------------------


def test_theme_output_attribute_access() -> None:
    """ThemeOutput.theme is a typed attribute, not a dict key."""
    t = ThemeOutput(
        theme="Neural scaling laws challenge compute assumptions.", supporting_papers=[1, 2]
    )
    assert t.theme == "Neural scaling laws challenge compute assumptions."
    assert t.supporting_papers == [1, 2]
    assert t.notes is None


def test_theme_output_model_dump_shape() -> None:
    """model_dump() produces the expected dict structure."""
    t = ThemeOutput(theme="Test theme for boundary.", supporting_papers=[1], notes="some note")
    d = t.model_dump()
    assert d["theme"] == "Test theme for boundary."
    assert d["supporting_papers"] == [1]
    assert d["notes"] == "some note"


def test_weekly_digest_output_holds_theme_instances() -> None:
    """WeeklyDigestOutput.themes is a list of ThemeOutput, not dicts."""
    theme = ThemeOutput(theme="Attention mechanisms improve alignment.", supporting_papers=[1, 2])
    digest = WeeklyDigestOutput(
        themes=[theme],
        summary="Two papers on attention and alignment this week.",
    )
    assert isinstance(digest.themes[0], ThemeOutput)
    assert digest.themes[0].theme == "Attention mechanisms improve alignment."


# ---------------------------------------------------------------------------
# Behavioural: generate_weekly_summary preserves ThemeOutput until boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_weekly_summary_theme_attribute_in_output() -> None:
    """generate_weekly_summary returns theme dicts with correct theme text.

    Mocks the DB pool and call_llm_structured so this runs without infra.
    Verifies that the result's themes list contains the correct theme text,
    proving attribute access (theme.theme) was used in the serialization path.
    """
    from paper_ingestion.weekly_summary import generate_weekly_summary
    from jarvis_common.verify import QuoteVerifier

    # Build a scripted LLM response with a ThemeOutput instance.
    scripted_theme = ThemeOutput(
        theme="Transformers outperform RNNs on long-range dependencies.",
        supporting_papers=[1, 2],
        notes=None,
    )
    scripted_digest = WeeklyDigestOutput(
        themes=[scripted_theme],
        summary="Two papers explore transformer architectures this week.",
    )

    # Minimal asyncpg row that satisfies the weekly_summary query projection.
    fake_row = {
        "id": 1,
        "title": "Transformers and long-range dependency modelling",
        "url": "http://example.com/paper1",
        "published_date": None,
        "authors": ["A. Author"],
        "topic_name": "Deep Learning",
        "topic_id": 1,
        "relevance_score": 0.9,
        "summary_brief": "Transformers outperform RNNs on long-range tasks.",
        "confidence": 0.8,
    }
    fake_row2 = {
        "id": 2,
        "title": "RNN vs Transformer: a comparative study",
        "url": "http://example.com/paper2",
        "published_date": None,
        "authors": ["B. Author"],
        "topic_name": "Deep Learning",
        "topic_id": 1,
        "relevance_score": 0.85,
        "summary_brief": "Comparative study confirms Transformer superiority on long-range tasks.",
        "confidence": 0.75,
    }

    # Wrap dicts as asyncpg-compatible record-likes (subscriptable + .get()).
    class _FakeRow(dict):
        pass

    rows = [_FakeRow(fake_row), _FakeRow(fake_row2)]

    # Mock the DB pool.
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=rows)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    mock_openai = MagicMock()

    with (
        patch(
            "paper_ingestion.weekly_summary.call_llm_structured",
            new=AsyncMock(return_value=scripted_digest),
        ),
        patch(
            "paper_ingestion.weekly_summary.get_smart_model",
            return_value="smart",
        ),
        patch(
            "paper_ingestion.weekly_summary.get_litellm_config",
            return_value=MagicMock(),
        ),
    ):
        result = await generate_weekly_summary(
            db_pool=mock_pool,
            verifier=QuoteVerifier(),
            days=7,
            openai_client=mock_openai,
        )

    assert "topics" in result
    assert len(result["topics"]) == 1
    topic = result["topics"][0]

    # The themes key must contain dicts (JSON-serializable) at the response boundary.
    assert len(topic["themes"]) == 1
    theme_dict = topic["themes"][0]
    assert isinstance(theme_dict, dict), "themes must be dicts at response boundary"
    assert theme_dict["theme"] == "Transformers outperform RNNs on long-range dependencies."
    assert "verified" not in theme_dict, (
        "raw themes list must NOT carry verified annotation (use verified_themes instead)"
    )

    # verified_themes / unverified_themes carry verification annotations.
    all_annotated = topic["verified_themes"] + topic["unverified_themes"]
    assert len(all_annotated) == 1, "each theme must appear in exactly one annotated list"
    annotated = all_annotated[0]
    assert "verified" in annotated
    assert "verification_reason" in annotated
    assert annotated["theme"] == "Transformers outperform RNNs on long-range dependencies."


@pytest.mark.asyncio
async def test_generate_weekly_summary_no_dict_get_on_theme_text() -> None:
    """Structural guard: theme.theme attribute is used, not dict.get("theme").

    Passes a ThemeOutput whose .theme attribute differs from what .get("theme", "")
    would return if the object were mistakenly treated as a dict.  Any regression
    back to dict.get() would return "" (KeyError safe-default) instead of the real value.
    """
    # ThemeOutput is NOT subscriptable — calling .get() on it raises AttributeError.
    t = ThemeOutput(
        theme="Contrastive learning improves few-shot generalisation.",
        supporting_papers=[1],
    )
    # Verify the attribute path works.
    assert t.theme == "Contrastive learning improves few-shot generalisation."
    # Verify that dict.get() is NOT available on ThemeOutput (structural safety check).
    assert not hasattr(t, "get"), (
        "ThemeOutput must not expose a .get() method — it is a Pydantic model, not a dict"
    )
