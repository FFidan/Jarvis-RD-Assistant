"""Weekly research summary generator.

Groups recent papers by topic and synthesizes cross-paper themes
using LLM analysis for each topic cluster.

Only papers the user has actively engaged with are included:
- Library UI engagement: paper_user_state.status IN ('starred', 'reading', 'read')
- Pulse card engagement: pulse_ratings.rating IN ('up', 'save', 'open') within the same window

This is the Model C (Complementary) guarantee: Weekly Summary reflects
what the user actually engaged with, not the full Pulse candidate firehose.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
from jarvis_common import get_smart_model
from jarvis_common.llm_client import (
    LITELLM_FALLBACK_ENV_NAMES,
    LLM_TIMEOUT_DEFAULT,
    ChatCompletionOptions,
    LiteLLMConfig,
    call_llm,
    get_litellm_config,
)
from jarvis_common.prompt_safety import escape_llm_text
from jarvis_common.time_utils import utc_now_iso

from paper_ingestion.extraction.verify import QuoteVerifier

logger = logging.getLogger(__name__)

DIGEST_PROMPT = """\
You are a research digest assistant. \
Analyze these {count} papers on the topic "{topic}" \
and identify 3-5 key themes or findings that emerge across them.

For each theme:
- State the theme clearly in one sentence
- Reference which papers support it by their number [Paper N]
- Note any contradictions or open questions

Papers:
{papers_context}

Respond in JSON format:
{{
    "themes": [
        {{
            "theme": "One-sentence theme description",
            "supporting_papers": [1, 3],
            "notes": "Optional additional context or contradictions"
        }}
    ],
    "summary": "2-3 sentence overview of the topic's state this week"
}}
"""


async def generate_weekly_summary(
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    litellm_url: str | None = None,
    days: int = 7,
    verifier: QuoteVerifier | None = None,
    user_id: int | None = None,
) -> dict:
    """Generate per-topic digests for papers the user engaged with in the lookback window.

    Only papers with active user engagement are included (Model C — Complementary):
    - Library UI: paper_user_state.status IN ('starred', 'reading', 'read')
    - Pulse cards: pulse_ratings.rating IN ('up', 'save', 'open') within the same window

    Papers passively surfaced by Pulse but never rated or saved are excluded,
    preventing the Weekly Summary from becoming a noise-generator.

    Parameters
    ----------
    user_id:
        When provided, restricts ``paper_user_state`` and ``pulse_ratings``
        lookups to the given user.  ``None`` (default) aggregates across all
        users, preserving backwards-compatible global behaviour.
    """
    litellm_config = get_litellm_config(fallback_env_names=LITELLM_FALLBACK_ENV_NAMES)
    if litellm_url is not None:
        litellm_config = LiteLLMConfig(
            base_url=litellm_url,
            api_key=litellm_config.api_key,
        )

    cutoff = datetime.now(UTC) - timedelta(days=days)

    # asyncpg.Pool has no .fetch() method — must acquire a connection first (PI-013).
    async with db_pool.acquire() as conn:
        smart_model = get_smart_model()
        rows = await conn.fetch(
            """
            SELECT p.id, p.title, p.url, p.published_date, p.authors,
                   t.name as topic_name, t.id as topic_id,
                   pt.relevance_score,
                   ps.summary_brief, ps.confidence
            FROM papers p
            JOIN paper_topics pt ON p.id = pt.paper_id
            JOIN topics t ON pt.topic_id = t.id
            LEFT JOIN paper_summaries ps ON p.id = ps.paper_id
            WHERE p.created_at >= $1
              AND (
                  EXISTS (
                      SELECT 1 FROM paper_user_state pus
                      WHERE pus.paper_id = p.id
                        AND (
                            COALESCE(pus.starred, FALSE)
                            OR pus.status IN ('starred', 'reading', 'read')
                        )
                        AND pus.user_id IS NOT DISTINCT FROM $2
                  )
                  OR
                  EXISTS (
                      SELECT 1 FROM pulse_ratings pr
                      WHERE pr.paper_id = p.id
                        AND pr.rating IN ('up', 'save', 'open')
                        AND pr.created_at >= $1
                        AND pr.user_id IS NOT DISTINCT FROM $2
                  )
              )
            ORDER BY t.name, pt.relevance_score DESC NULLS LAST
            """,
            cutoff,
            user_id,
        )

    # Empty-state honesty: short-circuit without calling the LLM.
    if not rows:
        logger.info(
            "weekly_summary: no engaged papers in the last %d days — skipping LLM synthesis",
            days,
        )
        return {
            "topics": [],
            "total_papers": 0,
            "period_start": cutoff.isoformat(),
            "period_end": utc_now_iso(),
            "message": (
                "No engaged papers in the last 7 days. "
                "Read or save some papers to see your weekly summary."
            ),
        }

    topics: dict[str, dict] = {}
    for row in rows:
        topic_name = row["topic_name"]
        if topic_name not in topics:
            topics[topic_name] = {"name": topic_name, "papers": []}
        topics[topic_name]["papers"].append(dict(row))

    result_topics: list[dict] = []
    total_papers: set[int] = set()

    for topic_name, topic_data in topics.items():
        papers = topic_data["papers"]
        total_papers.update(p["id"] for p in papers)

        papers_context = ""
        for i, p in enumerate(papers[:10], 1):
            brief = p.get("summary_brief") or p.get("title", "")
            papers_context += (
                f"\n[Paper {i}]: {escape_llm_text(p['title'])}"
                f"\n  Summary: {escape_llm_text(brief[:300])}\n"
            )

        themes: list[dict] = []
        summary = f"{len(papers)} papers on {topic_name} this week."

        if len(papers) >= 2:
            try:
                llm_data = await call_llm(
                    http_client,
                    DIGEST_PROMPT.format(
                        count=len(papers[:10]),
                        topic=escape_llm_text(topic_name),
                        papers_context=papers_context,
                    ),
                    options=ChatCompletionOptions(
                        model=smart_model,
                        max_tokens=600,
                        temperature=0.2,
                        timeout=LLM_TIMEOUT_DEFAULT,
                    ),
                    config=litellm_config,
                )
                themes = llm_data.get("themes", [])
                summary = llm_data.get("summary", summary)
            except Exception:
                # weekly_summary generation degrades to the default summary if synthesis fails.
                logger.exception("LLM weekly_summary generation failed for topic %s", topic_name)

        top_papers = [
            {
                "id": p["id"],
                "title": p["title"],
                "url": p["url"],
                "confidence": p.get("confidence"),
                "relevance_score": p.get("relevance_score"),
            }
            for p in papers[:5]
        ]

        # Verify each theme title against concatenated paper title+summary_brief
        # for the topic.  Cheap fuzzy match (~ms per theme) — ephemeral, not persisted.
        verified_themes: list[dict] = []
        unverified_themes: list[dict] = []
        if themes and verifier is not None:
            corpus_parts: list[str] = []
            for p in papers[:10]:
                corpus_parts.append(p.get("title") or "")
                brief = p.get("summary_brief") or ""
                if brief:
                    corpus_parts.append(brief)
            corpus = " ".join(part for part in corpus_parts if part).strip()

            for theme in themes:
                theme_text = str(theme.get("theme", "") or "").strip()
                if not theme_text or not corpus:
                    unverified_themes.append(theme)
                    continue
                try:
                    result = await asyncio.to_thread(verifier.verify_quote, theme_text, corpus, [])
                except Exception:
                    logger.warning("weekly_summary: theme verification raised", exc_info=True)
                    unverified_themes.append(theme)
                    continue
                if result.verified:
                    verified_themes.append(theme)
                else:
                    unverified_themes.append(theme)
        else:
            # No verifier wired, or no themes — treat themes as unverified.
            unverified_themes = list(themes)

        result_topics.append(
            {
                "name": topic_name,
                "paper_count": len(papers),
                "themes": themes,
                "verified_themes": verified_themes,
                "unverified_themes": unverified_themes,
                "top_papers": top_papers,
                "summary": summary,
            }
        )

    return {
        "topics": result_topics,
        "total_papers": len(total_papers),
        "period_start": cutoff.isoformat(),
        "period_end": utc_now_iso(),
    }
