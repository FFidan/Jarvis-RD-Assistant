"""Three-stage Pulse scoring pipeline.

Stage 1 — embedding_filter: cosine similarity + recency decay + author bonus.
Stage 2 — llm_rerank:       LLM scores relevance/novelty for top candidates.
Stage 3 — combine:          Weighted sum → final ranking.
"""

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
from jarvis_common.llm_client import (
    ChatCompletionOptions,
    request_chat_completion_content,
)

from app.models import PaperCreate
from app.pulse.profile import UserProfile
from app.pulse.prompts import build_scoring_prompt

logger = logging.getLogger(__name__)

_LLM_CONCURRENCY = 5
_LLM_MODEL = "smart"  # mistral-nemo: non-thinking model, reliable JSON output
_LLM_MAX_TOKENS = 512  # enough for reasoning + JSON; was 256 (too small for thinking models)
_LLM_TEMPERATURE = 0.0


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ScoredCandidate:
    """A candidate paper with accumulated signal scores from each pipeline stage."""

    paper: PaperCreate
    signals: dict[str, float]
    llm_relevance: int | None
    llm_novelty: int | None
    reasoning: str | None
    final_score: float | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors. Returns 0.0 on zero-norm."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _recency_decay(published_date: date | None, now: date | None = None) -> float:
    """Compute recency decay: exp(-age_days / 30), clamped to [0, 1]."""
    if published_date is None:
        return 0.0
    today = now or date.today()
    age_days = max(0, (today - published_date).days)
    return max(0.0, min(1.0, math.exp(-age_days / 30.0)))


# ---------------------------------------------------------------------------
# Stage 1
# ---------------------------------------------------------------------------


async def stage1_embedding_filter(
    candidates: list[PaperCreate],
    profile: UserProfile,
    embedder: Any,
    top_k: int = 50,
    now: date | None = None,
) -> list[ScoredCandidate]:
    """Filter and rank candidates using embedding similarity, recency, and author signals.

    Parameters
    ----------
    candidates:
        Raw candidate papers from source plugins.
    profile:
        UserProfile with library centroid, topics, and tracked author IDs.
    embedder:
        Embedder instance providing embed_texts().
    top_k:
        Maximum number of candidates to return.
    now:
        Reference date for recency calculations (defaults to today). Injected
        for testability.

    Returns
    -------
    list[ScoredCandidate]
        Top-k candidates sorted by preliminary score descending.
    """
    if not candidates:
        return []

    # Embed topics first (once) to compute topic_sim
    topic_embeddings: list[list[float]] = []
    if profile.topics:
        topic_texts = [f"{t.name} {t.description or ''}".strip() for t in profile.topics]
        try:
            topic_embeddings = await embedder.embed_texts(topic_texts)
        except Exception:
            logger.warning("stage1: failed to embed topics, topic_sim=0.0", exc_info=True)
            topic_embeddings = []

    # Embed candidate abstracts in one batch
    abstracts = [f"{c.title}. {c.abstract or ''}".strip() for c in candidates]
    try:
        candidate_embeddings = await embedder.embed_texts(abstracts)
    except Exception:
        logger.warning("stage1: failed to embed candidates", exc_info=True)
        # Return all with zero signals if embedding fails
        return [
            ScoredCandidate(
                paper=c,
                signals={"embedding": 0.0, "topic": 0.0, "recency": 0.0, "author_bonus": 0.0},
                llm_relevance=None,
                llm_novelty=None,
                reasoning=None,
                final_score=None,
            )
            for c in candidates
        ][:top_k]

    centroid = profile.library_centroid

    scored: list[ScoredCandidate] = []
    for idx, candidate in enumerate(candidates):
        cand_vec = candidate_embeddings[idx] if idx < len(candidate_embeddings) else []

        # Embedding similarity to library centroid
        embedding_sim = _cosine(cand_vec, centroid) if centroid else 0.0

        # Max topic similarity
        topic_sim = 0.0
        if topic_embeddings:
            topic_sim = max((_cosine(cand_vec, tv) for tv in topic_embeddings), default=0.0)

        # Recency decay
        recency = _recency_decay(candidate.published_date, now=now)

        # Author bonus: dual-set match — display names (lowercased) OR s2 numeric IDs
        author_bonus = 0.0
        if profile.tracked_author_names or profile.tracked_author_s2_ids:
            candidate_names = {a.lower() for a in candidate.authors}
            candidate_s2_ids = set(candidate.metadata.get("s2_author_ids", []))
            if (candidate_names & profile.tracked_author_names) or (
                candidate_s2_ids & profile.tracked_author_s2_ids
            ):
                author_bonus = 1.0

        signals = {
            "embedding": embedding_sim,
            "topic": topic_sim,
            "recency": recency,
            "author_bonus": author_bonus,
        }
        # Preliminary score (for ranking cut only)
        prelim = embedding_sim + topic_sim + recency + author_bonus * 0.5

        scored.append(
            ScoredCandidate(
                paper=candidate,
                signals=signals,
                llm_relevance=None,
                llm_novelty=None,
                reasoning=None,
                final_score=prelim,  # overwritten by stage3
            )
        )

    # Sort by preliminary score descending and keep top_k
    scored.sort(key=lambda sc: sc.final_score or 0.0, reverse=True)
    return scored[:top_k]


# ---------------------------------------------------------------------------
# Stage 2
# ---------------------------------------------------------------------------


async def stage2_llm_rerank(
    stage1_out: list[ScoredCandidate],
    profile: UserProfile,
    http_client: httpx.AsyncClient,
) -> list[ScoredCandidate]:
    """Score each candidate via LLM with bounded concurrency.

    Parameters
    ----------
    stage1_out:
        Candidates from stage1_embedding_filter.
    profile:
        UserProfile providing topic context and rating history.
    http_client:
        Shared httpx.AsyncClient for LiteLLM requests.

    Returns
    -------
    list[ScoredCandidate]
        Same list with llm_relevance, llm_novelty, reasoning, and signals
        filled in. Gracefully degrades: failed candidates keep None scores.
    """
    if not stage1_out:
        return []

    semaphore = asyncio.Semaphore(_LLM_CONCURRENCY)

    async def _score_one(sc: ScoredCandidate) -> ScoredCandidate:
        async with semaphore:
            try:
                messages = build_scoring_prompt(
                    topic_context=profile.topics,
                    positive_examples=profile.recent_positive_titles,
                    negative_examples=profile.recent_negative_titles,
                    candidate=sc.paper,
                )
                options = ChatCompletionOptions(
                    model=_LLM_MODEL,
                    max_tokens=_LLM_MAX_TOKENS,
                    temperature=_LLM_TEMPERATURE,
                    response_format={"type": "json_object"},
                )
                raw = await request_chat_completion_content(
                    http_client,
                    messages=messages,
                    options=options,
                )
                # Parse JSON response
                import json as _json

                parsed = _json.loads(raw)
                relevance = int(parsed["relevance"])
                novelty = int(parsed["novelty"])
                reasoning = str(parsed.get("reasoning", ""))

                # Clamp to valid range
                relevance = max(1, min(10, relevance))
                novelty = max(1, min(10, novelty))

                new_signals = dict(sc.signals)
                new_signals["llm_relevance"] = relevance / 10.0
                new_signals["llm_novelty"] = novelty / 10.0

                return ScoredCandidate(
                    paper=sc.paper,
                    signals=new_signals,
                    llm_relevance=relevance,
                    llm_novelty=novelty,
                    reasoning=reasoning,
                    final_score=sc.final_score,
                )
            except Exception:
                logger.warning("stage2: LLM scoring failed for %r", sc.paper.title, exc_info=True)
                return ScoredCandidate(
                    paper=sc.paper,
                    signals=sc.signals,
                    llm_relevance=None,
                    llm_novelty=None,
                    reasoning="LLM scoring failed",
                    final_score=sc.final_score,
                )

    results = await asyncio.gather(*[_score_one(sc) for sc in stage1_out])
    return list(results)


# ---------------------------------------------------------------------------
# Stage 3
# ---------------------------------------------------------------------------


async def stage3_combine(
    stage2_out: list[ScoredCandidate],
    weights: dict[str, float],
) -> list[ScoredCandidate]:
    """Compute weighted-sum final score and sort candidates descending.

    Parameters
    ----------
    stage2_out:
        Candidates from stage2_llm_rerank.
    weights:
        Signal name → weight mapping. Missing signals are treated as 0.0.

    Returns
    -------
    list[ScoredCandidate]
        Candidates sorted by final_score descending with final_score populated.
    """
    result: list[ScoredCandidate] = []
    for sc in stage2_out:
        final = sum(sc.signals.get(k, 0.0) * w for k, w in weights.items())
        result.append(
            ScoredCandidate(
                paper=sc.paper,
                signals=sc.signals,
                llm_relevance=sc.llm_relevance,
                llm_novelty=sc.llm_novelty,
                reasoning=sc.reasoning,
                final_score=final,
            )
        )
    result.sort(key=lambda sc: sc.final_score or 0.0, reverse=True)
    return result
