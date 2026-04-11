"""Discovery & Pulse subsystem — proactive overnight paper scoring pipeline."""

from app.pulse.deck import assemble_deck, load_history, load_today, persist_deck
from app.pulse.profile import UserProfile, load_profile
from app.pulse.prompts import PULSE_SCORING_SYSTEM_PROMPT, build_scoring_prompt
from app.pulse.scoring import (
    ScoredCandidate,
    stage1_embedding_filter,
    stage2_llm_rerank,
    stage3_combine,
)

__all__ = [
    "UserProfile",
    "load_profile",
    "stage1_embedding_filter",
    "stage2_llm_rerank",
    "stage3_combine",
    "ScoredCandidate",
    "assemble_deck",
    "persist_deck",
    "load_today",
    "load_history",
    "build_scoring_prompt",
    "PULSE_SCORING_SYSTEM_PROMPT",
]
