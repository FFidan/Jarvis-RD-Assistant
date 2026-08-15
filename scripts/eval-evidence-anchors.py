#!/usr/bin/env python3
"""Measure where model-generated evidence anchors land in representative papers.

This manual benchmark calls the production findings generator and quote verifier.
It requires a reachable LiteLLM instance and is intentionally not part of CI.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# The package roots are on the path under pytest but not for a bare script run,
# so bootstrap them the way the sibling scripts do; without this the documented
# invocation fails on `import jarvis_common`.
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _package_root in (
    _REPO_ROOT / "libs" / "jarvis_common",
    _REPO_ROOT / "services" / "paper_ingestion",
):
    _package_root_str = str(_package_root)
    if _package_root_str not in sys.path:
        sys.path.insert(0, _package_root_str)

import instructor  # noqa: E402
import openai  # noqa: E402
from jarvis_common import get_smart_model  # noqa: E402
from jarvis_common.app_factory import STRUCTURED_DECODING_MODE
from jarvis_common.llm_client import get_litellm_config  # noqa: E402
from jarvis_common.prompt_safety import wrap_delimited
from jarvis_common.settings import get_secrets_settings  # noqa: E402
from jarvis_common.verify import QuoteVerifier
from paper_ingestion.models import ChunkResponse, KeyFinding  # noqa: E402
from paper_ingestion.services.summarization import (  # noqa: E402
    _SUMMARY_OUTPUT_TOKENS,
    _SYSTEM_SUMMARIZE,
    SUMMARIZE_PROMPT_TEMPLATE,
    _call_summarize_llm,
    _findings_system_prompt,
)
from paper_ingestion.services.summarization_models import SummarizationOutput  # noqa: E402

MIN_BODY_ANCHOR_RATE = 0.80
MIN_PREFERRED_SECTION_RATE = 0.60
PREFERRED_SECTIONS = frozenset({"methods", "results"})
_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FixturePassage:
    section: str
    text: str
    page: int


@dataclass(frozen=True, slots=True)
class FixturePaper:
    name: str
    title: str
    authors: str
    passages: tuple[FixturePassage, ...]


FIXTURES = (
    FixturePaper(
        name="controlled retrieval study",
        title="Calibrated Retrieval for Scientific Questions",
        authors="A. Rivera and M. Chen",
        passages=(
            FixturePassage(
                "abstract",
                "We summarize a calibrated retrieval approach for scientific question answering.",
                1,
            ),
            FixturePassage(
                "methods",
                "Methods: We compared the calibrated retriever with BM25 on five seeded corpora.",
                3,
            ),
            FixturePassage(
                "results",
                "Results: Calibration increased exact evidence recall from 0.61 to 0.78.",
                7,
            ),
            FixturePassage(
                "discussion",
                "Discussion: The improvement was consistent across four of the five corpora.",
                9,
            ),
        ),
    ),
    FixturePaper(
        name="laboratory replication",
        title="Replicating a Catalyst Stability Protocol",
        authors="S. Okafor and J. Meyer",
        passages=(
            FixturePassage(
                "abstract",
                "This paper summarizes a replication of a catalyst stability protocol.",
                1,
            ),
            FixturePassage(
                "methods",
                "Methods: Each catalyst completed twelve thermal cycles under blinded labeling.",
                4,
            ),
            FixturePassage(
                "results",
                "Results: Nine of ten catalysts retained more than 95 percent activity.",
                8,
            ),
            FixturePassage(
                "limitations",
                "Limitations: The study used one reactor geometry and a single carrier gas.",
                10,
            ),
        ),
    ),
    FixturePaper(
        name="abstract-only record",
        title="A Short Report Without Full Text",
        authors="L. Novak",
        passages=(
            FixturePassage(
                "abstract",
                "The abstract reports that adaptive sampling reduced measurement time "
                "by 18 percent.",
                1,
            ),
        ),
    ),
)


def _chunks(fixture_number: int, fixture: FixturePaper) -> list[ChunkResponse]:
    return [
        ChunkResponse(
            id=fixture_number * 100 + index,
            paper_id=fixture_number,
            chunk_index=index,
            content=passage.text,
            page_number=passage.page,
            created_at=_CREATED_AT,
        )
        for index, passage in enumerate(fixture.passages)
    ]


async def _evaluate(model: str) -> bool:
    config = get_litellm_config()
    secret = get_secrets_settings().litellm_master_key
    raw_client = openai.AsyncOpenAI(
        base_url=f"{config.base_url}/v1",
        api_key=secret.get_secret_value() if secret else "dummy",
    )
    client = instructor.from_openai(
        raw_client,
        mode=instructor.Mode[STRUCTURED_DECODING_MODE],
    )

    distribution: Counter[str] = Counter()
    candidate_count = 0
    verified_count = 0
    body_fixture_anchors = 0
    body_fixture_body_anchors = 0
    preferred_anchors = 0
    abstract_only_verified = 0

    try:
        for fixture_number, fixture in enumerate(FIXTURES, start=1):
            chunks = _chunks(fixture_number, fixture)
            full_text = "\n\n".join(chunk.content for chunk in chunks)
            title, _ = wrap_delimited("title", fixture.title)
            authors, _ = wrap_delimited("authors", fixture.authors)
            text, _ = wrap_delimited("paper_text", full_text)
            generated = await _call_summarize_llm(
                client,
                response_model=SummarizationOutput,
                prompt=SUMMARIZE_PROMPT_TEMPLATE.format(
                    title=title,
                    authors=authors,
                    text=text,
                ),
                system=_findings_system_prompt(_SYSTEM_SUMMARIZE, chunks),
                max_tokens=_SUMMARY_OUTPUT_TOKENS,
                model=model,
                paper_id=fixture_number,
            )
            findings = [
                KeyFinding(
                    finding=item.finding,
                    quote=item.quote,
                    page_number=item.page_number,
                )
                for item in generated.key_findings
            ]
            report = QuoteVerifier().verify_findings(findings, full_text, chunks)
            candidate_count += report.total_findings
            verified_count += report.verified_count

            has_body = len(chunks) > 1
            for result in report.results:
                if not result.verified or result.chunk_index is None:
                    continue
                section = fixture.passages[result.chunk_index].section
                distribution[section] += 1
                if has_body:
                    body_fixture_anchors += 1
                    if section != "abstract":
                        body_fixture_body_anchors += 1
                        if section in PREFERRED_SECTIONS:
                            preferred_anchors += 1
                else:
                    abstract_only_verified += 1
    finally:
        await raw_client.close()

    body_rate = body_fixture_body_anchors / body_fixture_anchors if body_fixture_anchors else 0.0
    preferred_rate = (
        preferred_anchors / body_fixture_body_anchors if body_fixture_body_anchors else 0.0
    )
    accepted = (
        body_rate >= MIN_BODY_ANCHOR_RATE
        and preferred_rate >= MIN_PREFERRED_SECTION_RATE
        and abstract_only_verified >= 1
    )

    print(f"Model: {model}")
    print(f"Candidates: {candidate_count}; verified: {verified_count}")
    print("Verified anchor distribution:")
    for section in sorted(distribution):
        print(f"  {section}: {distribution[section]}")
    print(f"Body-section rate: {body_rate:.1%} (required {MIN_BODY_ANCHOR_RATE:.0%})")
    print(f"Methods/results rate: {preferred_rate:.1%} (required {MIN_PREFERRED_SECTION_RATE:.0%})")
    print(f"Abstract-only verified anchors: {abstract_only_verified} (required at least 1)")
    print(f"Acceptance: {'PASS' if accepted else 'FAIL'}")
    return accepted


def main(argv: list[str] | None = None) -> int:
    """Run the evidence-anchor benchmark.

    Parameters
    ----------
    argv : list[str] | None
        Optional command-line arguments.

    Returns
    -------
    int
        Zero when the documented acceptance bar is met, otherwise one.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=get_smart_model())
    args = parser.parse_args(argv)
    return 0 if asyncio.run(_evaluate(args.model)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
