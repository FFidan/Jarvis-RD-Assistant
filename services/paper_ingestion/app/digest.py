"""Weekly research digest generator.

Groups recent papers by topic and synthesizes cross-paper themes
using LLM analysis for each topic cluster.
"""

import logging
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx

from jarvis_common import get_smart_model
from jarvis_common.llm_client import (
    ChatCompletionOptions,
    LLM_TIMEOUT_DEFAULT,
    LITELLM_FALLBACK_ENV_NAMES,
    LiteLLMConfig,
    call_llm,
    get_litellm_config,
)

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


async def generate_weekly_digest(
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    litellm_url: str | None = None,
    days: int = 7,
) -> dict:
    """Generate per-topic digests for papers ingested within the lookback window."""
    litellm_config = get_litellm_config(
        fallback_env_names=LITELLM_FALLBACK_ENV_NAMES
    )
    if litellm_url is not None:
        litellm_config = LiteLLMConfig(
            base_url=litellm_url,
            api_key=litellm_config.api_key,
        )

    cutoff = datetime.now(UTC) - timedelta(days=days)

    # asyncpg.Pool has no .fetch() method — must acquire a connection first (PI-013).
    async with db_pool.acquire() as conn:
        smart_model = await get_smart_model(conn)
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
            ORDER BY t.name, pt.relevance_score DESC NULLS LAST
            """,
            cutoff,
        )

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
            papers_context += f"\n[Paper {i}]: {p['title']}\n  Summary: {brief[:300]}\n"

        themes: list[dict] = []
        summary = f"{len(papers)} papers on {topic_name} this week."

        if len(papers) >= 2:
            try:
                llm_data = await call_llm(
                    http_client,
                    DIGEST_PROMPT.format(
                        count=len(papers[:10]),
                        topic=topic_name,
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
                # Digest generation degrades to the default summary if synthesis fails.
                logger.exception("LLM digest generation failed for topic %s", topic_name)

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

        result_topics.append(
            {
                "name": topic_name,
                "paper_count": len(papers),
                "themes": themes,
                "top_papers": top_papers,
                "summary": summary,
            }
        )

    return {
        "topics": result_topics,
        "total_papers": len(total_papers),
        "period_start": cutoff.isoformat(),
        "period_end": datetime.now(UTC).isoformat(),
    }
