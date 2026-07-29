"""Row-to-model conversion helpers for the learning engine service."""

from learning_engine.models import CardResponse, DeckResponse, Evidence


def row_to_deck_response(row) -> DeckResponse:
    """Build a DeckResponse from a JOIN row that aggregates card counts.

    ``card_count`` and ``due_count`` may be NULL when no cards exist;
    both default to 0 to keep callers free of None-checks.
    """
    return DeckResponse(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        topic_id=row["topic_id"],
        card_count=row.get("card_count", 0) or 0,
        due_count=row.get("due_count", 0) or 0,
        created_at=row["created_at"],
    )


def row_to_card_response(row) -> CardResponse:
    """Build a CardResponse from a cards table row.

    ``evidence`` is set to None when the JSONB column is NULL; ``fsrs_state``
    falls back to an empty dict so downstream FSRS logic never receives None.
    """
    evidence_raw = row["evidence"]
    evidence = (
        Evidence(**evidence_raw)
        if evidence_raw is not None and isinstance(evidence_raw, dict)
        else None
    )

    return CardResponse(
        id=row["id"],
        deck_id=row["deck_id"],
        paper_id=row["paper_id"],
        card_type=row["card_type"],
        front=row["front"],
        back=row["back"],
        evidence=evidence,
        fsrs_state=row["fsrs_state"] or {},
        due_at=row["due_at"],
        stale=bool(row.get("stale", False)),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
