"""Prompt templates for the Pulse Stage 2 LLM scoring step.

Provides a version-controlled system prompt and a builder function that
assembles the chat completion message list for a single candidate paper.
"""

from jarvis_common.prompt_safety import safe_for_prompt

from app.models import PaperCreate, TopicRef

_ABSTRACT_MAX_CHARS = 1500
_AUTHORS_MAX = 5

PULSE_SCORING_SYSTEM_PROMPT = """\
You are a research paper relevance scoring assistant for a researcher's
personal knowledge management system. Your task is to evaluate how relevant
and novel a candidate paper is given the researcher's active interests.

Score each paper on two dimensions:
- relevance (1-10): How closely does this paper address the researcher's topics?
  1 = completely unrelated, 10 = directly addresses a core research interest.
- novelty (1-10): Does this paper present new methods, findings, or perspectives?
  1 = review/survey of known material, 10 = genuinely novel contribution.

Respond ONLY with valid JSON in this exact format:
{"relevance": <integer 1-10>, "novelty": <integer 1-10>, "reasoning": "<one sentence>"}

Do not include any text outside the JSON object. The reasoning field must be
a single sentence explaining the most important factor in your scoring."""


def build_scoring_prompt(
    topic_context: list[TopicRef],
    positive_examples: list[str],
    negative_examples: list[str],
    candidate: PaperCreate,
) -> list[dict[str, str]]:
    """Build chat completion messages for LLM scoring of a candidate paper.

    Parameters
    ----------
    topic_context:
        List of active research topics (name + optional description).
    positive_examples:
        Recent paper titles the researcher rated positively.
    negative_examples:
        Recent paper titles the researcher rated negatively.
    candidate:
        The paper to be scored.

    Returns
    -------
    list[dict]
        Two-element list: system message then user message.
    """
    # --- Research topics section ---
    if topic_context:
        topic_lines = []
        for t in topic_context:
            if t.description:
                topic_lines.append(f"- {safe_for_prompt(t.name)}: {safe_for_prompt(t.description)}")
            else:
                topic_lines.append(f"- {safe_for_prompt(t.name)}")
        topics_section = "Research topics:\n" + "\n".join(topic_lines)
    else:
        topics_section = "Research topics: (none specified)"

    # --- Positive examples ---
    if positive_examples:
        pos_lines = "\n".join(f"- {safe_for_prompt(t)}" for t in positive_examples)
        pos_section = f"Recently liked papers (high relevance examples):\n{pos_lines}"
    else:
        pos_section = "Recently liked papers: (none yet)"

    # --- Negative examples ---
    if negative_examples:
        neg_lines = "\n".join(f"- {safe_for_prompt(t)}" for t in negative_examples)
        neg_section = f"Recently dismissed papers (low relevance examples):\n{neg_lines}"
    else:
        neg_section = "Recently dismissed papers: (none yet)"

    # --- Candidate paper ---
    authors_display = candidate.authors[:_AUTHORS_MAX]
    authors_str = safe_for_prompt(", ".join(authors_display))

    abstract = candidate.abstract or ""
    if len(abstract) > _ABSTRACT_MAX_CHARS:
        abstract = abstract[:_ABSTRACT_MAX_CHARS] + "..."
    abstract = safe_for_prompt(abstract)

    safe_title = safe_for_prompt(candidate.title)

    user_content = f"""\
{topics_section}

{pos_section}

{neg_section}

Candidate paper to score:
Title: {safe_title}
Authors: {authors_str}
Abstract: {abstract}

Score this paper. Return JSON only:
{{"relevance": <1-10>, "novelty": <1-10>, "reasoning": "<one sentence>"}}"""

    return [
        {"role": "system", "content": PULSE_SCORING_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
