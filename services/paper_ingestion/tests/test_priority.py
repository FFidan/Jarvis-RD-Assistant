"""Tests for priority scoring logic."""

from datetime import UTC, datetime, timedelta

from app.models import compute_priority, priority_level

# ---------------------------------------------------------------------------
# compute_priority tests
# ---------------------------------------------------------------------------


def test_compute_priority_all_zeros():
    """All zero inputs produce minimum score."""
    now = datetime.now(UTC)
    score = compute_priority([], None, 0, now)
    # relevance=0, recency=0.5 (default), citation_boost=0
    # 0.5*0 + 0.3*0.5 + 0.2*0 = 0.15
    assert score == 0.15


def test_compute_priority_high_relevance():
    """High relevance score dominates the priority."""
    now = datetime.now(UTC)
    score = compute_priority([0.9, 0.5, 0.3], now, 0, now)
    # relevance=0.9, recency=1.0 (0 days old), citation_boost=0
    # 0.5*0.9 + 0.3*1.0 + 0.2*0 = 0.75
    assert score == 0.75


def test_compute_priority_old_paper():
    """Paper older than 30 days gets zero recency."""
    now = datetime.now(UTC)
    discovered = now - timedelta(days=60)
    score = compute_priority([0.5], discovered, 0, now)
    # relevance=0.5, recency=max(0, 1-60/30)=0.0, citation_boost=0
    # 0.5*0.5 + 0.3*0.0 + 0.2*0 = 0.25
    assert score == 0.25


def test_compute_priority_high_citations():
    """High citation count boosts the score."""
    now = datetime.now(UTC)
    score = compute_priority([], None, 200, now)
    # relevance=0, recency=0.5, citation_boost=min(1.0, 200/100)=1.0
    # 0.5*0 + 0.3*0.5 + 0.2*1.0 = 0.35
    assert score == 0.35


def test_compute_priority_perfect_score():
    """Perfect inputs produce maximum score."""
    now = datetime.now(UTC)
    score = compute_priority([1.0], now, 100, now)
    # relevance=1.0, recency=1.0, citation_boost=1.0
    # 0.5*1.0 + 0.3*1.0 + 0.2*1.0 = 1.0
    assert score == 1.0


def test_compute_priority_moderate_case():
    """Moderate inputs produce a mid-range score."""
    now = datetime.now(UTC)
    discovered = now - timedelta(days=15)
    score = compute_priority([0.6], discovered, 50, now)
    # relevance=0.6, recency=max(0, 1-15/30)=0.5, citation_boost=50/100=0.5
    # 0.5*0.6 + 0.3*0.5 + 0.2*0.5 = 0.55
    assert score == 0.55


def test_compute_priority_none_citation_count():
    """None citation_count treated as zero."""
    now = datetime.now(UTC)
    score = compute_priority([0.5], now, None, now)
    # relevance=0.5, recency=1.0, citation_boost=0
    # 0.5*0.5 + 0.3*1.0 + 0.2*0 = 0.55
    assert score == 0.55


def test_compute_priority_empty_relevance_scores():
    """Empty relevance_scores list uses 0.0 relevance."""
    now = datetime.now(UTC)
    score = compute_priority([], now, 50, now)
    # relevance=0, recency=1.0, citation_boost=0.5
    # 0.5*0 + 0.3*1.0 + 0.2*0.5 = 0.4
    assert score == 0.4


def test_compute_priority_uses_max_relevance():
    """Multiple relevance scores -- max is used."""
    now = datetime.now(UTC)
    score_multi = compute_priority([0.3, 0.8, 0.5], now, 0, now)
    score_single = compute_priority([0.8], now, 0, now)
    assert score_multi == score_single


# ---------------------------------------------------------------------------
# priority_level tests
# ---------------------------------------------------------------------------


def test_priority_level_none():
    """None score returns 'unscored'."""
    assert priority_level(None) == "unscored"


def test_priority_level_must_read():
    """Score > 0.7 is must-read."""
    assert priority_level(0.71) == "must-read"
    assert priority_level(0.9) == "must-read"
    assert priority_level(1.0) == "must-read"


def test_priority_level_recommended():
    """Score 0.4 < score <= 0.7 is recommended."""
    assert priority_level(0.41) == "recommended"
    assert priority_level(0.55) == "recommended"
    assert priority_level(0.7) == "recommended"


def test_priority_level_background():
    """Score <= 0.4 is background."""
    assert priority_level(0.0) == "background"
    assert priority_level(0.2) == "background"
    assert priority_level(0.4) == "background"


def test_priority_level_boundary_0_7():
    """Boundary at 0.7 is recommended, not must-read."""
    assert priority_level(0.7) == "recommended"
    assert priority_level(0.7001) == "must-read"


def test_priority_level_boundary_0_4():
    """Boundary at 0.4 is background, not recommended."""
    assert priority_level(0.4) == "background"
    assert priority_level(0.4001) == "recommended"
