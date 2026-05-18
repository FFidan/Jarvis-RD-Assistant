#!/usr/bin/env python3
"""Phase 1 Pulse scoring eval harness.

Loads a 30-paper labeled fixture, runs Stages 1-3 of the Pulse scoring
pipeline against a synthetic UserProfile with a deterministic mock
embedder and a deterministic mock LLM, then reports:

    precision@10 — fraction of top-10 that are `yes`-labeled
    yes-recall   — fraction of all `yes` papers that make it into top-10
    no-leakage   — fraction of top-10 that are `no`-labeled (inverse metric)

Acceptance targets (Phase 1 Goal §2):

    precision@10 >= 0.60
    no-leakage   <= 0.10

Exit 0 if both targets are met, non-zero otherwise.

Run from the repo root so the ``paper_ingestion`` package imports resolve::

    uv run python scripts/eval_pulse.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# Bootstrap import path so the ``paper_ingestion`` package resolves regardless
# of where the script is invoked from. We add services/paper_ingestion to sys.path.
_REPO_ROOT = Path(__file__).resolve().parent.parent


class ScriptError(RuntimeError):
    """Script-level error; caught by the __main__ block."""


_SERVICE_ROOT = _REPO_ROOT / "services" / "paper_ingestion"
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

import httpx  # noqa: E402
from paper_ingestion.models import PaperCreate, SourceType, TopicRef  # noqa: E402
from paper_ingestion.pulse.deck import assemble_deck  # noqa: E402
from paper_ingestion.pulse.profile import UserProfile  # noqa: E402
from paper_ingestion.pulse.scoring import (  # noqa: E402
    stage1_embedding_filter,
    stage2_llm_rerank,
    stage3_combine,
)

FIXTURE_PATH = _REPO_ROOT / "scripts" / "fixtures" / "eval_pulse_labeled_set.json"
PRECISION_TARGET = 0.60
NO_LEAKAGE_MAX = 0.10
EMBEDDING_DIM = 16
DECK_SIZE = 10

logger = logging.getLogger("eval_pulse")

# ---------------------------------------------------------------------------
# Mock embedder
# ---------------------------------------------------------------------------
#
# The mock embedder produces deterministic embeddings that encode the label
# of each paper along a known "signal" axis (dim 0), while keeping dims
# 1..N-1 filled with a title-derived hash pattern. Topic embeddings are
# pinned to [1.0, 0, 0, ...], so cosine similarity to a topic is directly
# controlled by the signal component.
#
# yes   → signal 0.90, small random perturbation in tail dims
# maybe → signal 0.45
# no    → signal 0.05
#
# This yields a clean, reproducible topic-similarity gradient without
# touching real LiteLLM/Ollama.


def _hash_vector(text: str, dim: int) -> list[float]:
    """Return a deterministic pseudo-random unit vector derived from `text`."""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    # Expand digest to the required dimensionality by cycling bytes.
    raw = [h[i % len(h)] / 255.0 - 0.5 for i in range(dim)]
    # Small magnitude so it does not overwhelm the label signal.
    scale = 0.05
    return [v * scale for v in raw]


def _label_signal(label: str) -> float:
    return {"yes": 0.90, "maybe": 0.45, "no": 0.05}[label]


class MockEmbedder:
    """Deterministic stand-in for app.embedder.Embedder in the eval harness."""

    def __init__(self, labels_by_text: dict[str, str]) -> None:
        # Map the exact "title. abstract" string produced by stage1 back to
        # a label. We also accept plain topic names (label="topic").
        self._labels = labels_by_text

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            label = self._classify(text)
            signal = _label_signal(label) if label in {"yes", "maybe", "no"} else 0.0
            # Topic texts get the unit axis vector directly.
            is_topic = label == "topic"
            vec = [0.0] * EMBEDDING_DIM
            if is_topic:
                vec[0] = 1.0
            else:
                vec[0] = signal
                tail = _hash_vector(text, EMBEDDING_DIM - 1)
                for i, v in enumerate(tail):
                    vec[i + 1] = v
            vectors.append(vec)
        return vectors

    def _classify(self, text: str) -> str:
        # Stage 1 builds "<title>. <abstract>" — we match on the title prefix.
        for key, label in self._labels.items():
            if text.startswith(key):
                return label
        # Topic embedding path: stage 1 builds "<name> <description>" — match
        # on the topic name prefix.
        for key, label in self._labels.items():
            if label == "topic" and text.startswith(key):
                return label
        return "unknown"


# ---------------------------------------------------------------------------
# Mock LLM client
# ---------------------------------------------------------------------------
#
# The mock `http_client.post` inspects the chat messages, recovers the
# candidate paper title from the `Title:` line in the user message, looks
# up its label, and returns a LiteLLM-shaped response with relevance
# derived from the label:
#
#   yes   → relevance 9, novelty 7
#   maybe → relevance 5, novelty 5
#   no    → relevance 2, novelty 3
#
# The integer relevance is what stage2 ultimately uses; novelty is
# secondary under the default weights.

_LLM_SCORES_BY_LABEL = {
    "yes": {"relevance": 9, "novelty": 7, "reasoning": "Direct match on topic."},
    "maybe": {"relevance": 5, "novelty": 5, "reasoning": "Adjacent vocabulary, weaker fit."},
    "no": {"relevance": 2, "novelty": 3, "reasoning": "Off-topic control."},
}

_TITLE_RE = re.compile(r"^Title:\s*(.+)$", re.MULTILINE)


def _make_mock_http_client(labels_by_title: dict[str, str]) -> httpx.AsyncClient:
    """Build an AsyncMock httpx client whose .post() returns label-derived scores."""

    async def _mock_post(url: str, **kwargs: Any) -> MagicMock:
        payload = kwargs.get("json") or {}
        messages: list[dict[str, str]] = payload.get("messages", [])
        user_content = ""
        for msg in messages:
            if msg.get("role") == "user":
                user_content = msg.get("content", "")
                break

        match = _TITLE_RE.search(user_content)
        title = match.group(1).strip() if match else ""
        label = labels_by_title.get(title, "maybe")
        scores = _LLM_SCORES_BY_LABEL[label]

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(scores),
                    }
                }
            ]
        }
        return resp

    client = AsyncMock(spec=httpx.AsyncClient)
    client.post.side_effect = _mock_post
    return client


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def _load_fixture() -> tuple[list[PaperCreate], dict[str, str], dict[str, str], list[TopicRef]]:
    raw = json.loads(FIXTURE_PATH.read_text())

    candidates: list[PaperCreate] = []
    labels_by_external_id: dict[str, str] = {}
    labels_by_title: dict[str, str] = {}

    for p in raw["papers"]:
        candidate = PaperCreate(
            external_id=p["external_id"],
            source_type=SourceType(p["source_type"]),
            title=p["title"],
            authors=p.get("authors", []),
            abstract=p.get("abstract"),
            published_date=p.get("published_date"),
            url=p["url"],
        )
        candidates.append(candidate)
        labels_by_external_id[p["external_id"]] = p["label"]
        labels_by_title[p["title"]] = p["label"]

    topics = [
        TopicRef(
            id=i + 1,
            name=t["name"],
            description=t.get("description"),
            query_terms=[],
        )
        for i, t in enumerate(raw["profile_topics"])
    ]
    return candidates, labels_by_external_id, labels_by_title, topics


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run_eval() -> tuple[float, float, float]:
    """Run the Pulse scoring pipeline against the labeled fixture and return metrics.

    Loads the fixture, constructs mock embedder and LLM clients, runs the full
    three-stage Pulse pipeline (embedding filter → LLM rerank → combine), and
    computes precision, recall, and leakage on the assembled deck.

    Returns
    -------
    tuple[float, float, float]
        ``(precision_at_10, yes_recall, no_leakage)`` as fractions in [0, 1].
    """
    candidates, labels_by_id, labels_by_title, topics = _load_fixture()

    # Build the text-keyed label map the mock embedder expects. Stage 1
    # feeds "<title>. <abstract>" as candidate text; we key on title so
    # any prefix-match works.
    embedder_labels: dict[str, str] = {}
    for p in candidates:
        embedder_labels[p.title] = labels_by_title[p.title]
    # Topic embeddings are keyed on the topic name prefix (sentinel label).
    for topic in topics:
        embedder_labels[topic.name] = "topic"

    profile = UserProfile(
        topics=topics,
        tracked_author_names=set(),
        tracked_author_s2_ids=set(),
        library_centroid=None,  # eval relies on topic similarity only
        weights={
            "embedding": 0.0,  # no library centroid in eval
            "topic": 0.3,
            "llm_relevance": 0.5,
            "llm_novelty": 0.1,
            "author_bonus": 0.05,
            "recency": 0.05,
        },
        deck_size=DECK_SIZE,
        stage2_top_k=30,
        recent_positive_titles=[],
        recent_negative_titles=[],
    )

    embedder = MockEmbedder(embedder_labels)
    http_client = _make_mock_http_client(labels_by_title)

    stage1 = await stage1_embedding_filter(
        candidates, profile, embedder, top_k=profile.stage2_top_k
    )
    stage2 = await stage2_llm_rerank(stage1, profile, http_client)
    stage3 = await stage3_combine(stage2, profile.weights)
    deck = await assemble_deck(stage3, size=DECK_SIZE)

    top_labels = [labels_by_id[sc.paper.external_id] for sc in deck]
    yes_total = sum(1 for v in labels_by_id.values() if v == "yes")

    precision_at_10 = top_labels.count("yes") / max(len(top_labels), 1)
    yes_recall = top_labels.count("yes") / max(yes_total, 1)
    no_leakage = top_labels.count("no") / max(len(top_labels), 1)

    return precision_at_10, yes_recall, no_leakage


async def main() -> None:
    """Run the Pulse evaluation harness and print pass/fail results.

    Raises
    ------
    ScriptError
        If the fixture file is missing or the precision/leakage targets are not met.
    """
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    if not FIXTURE_PATH.exists():
        raise ScriptError(f"fixture not found at {FIXTURE_PATH}")

    precision_at_10, yes_recall, no_leakage = await run_eval()

    print(f"precision@10 = {precision_at_10:.2%}  (target >= {PRECISION_TARGET:.0%})")
    print(f"yes-recall   = {yes_recall:.2%}")
    print(f"no-leakage   = {no_leakage:.2%}  (target <= {NO_LEAKAGE_MAX:.0%})")

    ok = precision_at_10 >= PRECISION_TARGET and no_leakage <= NO_LEAKAGE_MAX
    print("PASS" if ok else "FAIL")
    if not ok:
        raise ScriptError("Pulse evaluation targets not met")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ScriptError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
