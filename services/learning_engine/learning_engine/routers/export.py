"""Anki export endpoint."""

import io
import re

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from jarvis_common.auth import current_user_id_strict

from learning_engine.anki_exporter import AnkiExporter
from learning_engine.card_store import CURRENT_CARD_SQL
from learning_engine.deps import get_anki_exporter, get_db_pool, limiter

router = APIRouter(prefix="/api/export", tags=["export"])


@router.get("/anki/{deck_id}")
@limiter.limit("10/minute")
async def export_anki(
    request: Request,
    deck_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    anki_exporter: AnkiExporter = Depends(get_anki_exporter),
    user_id: int = Depends(current_user_id_strict),
) -> StreamingResponse:
    """Export a deck as an Anki .apkg file."""
    async with db_pool.acquire() as conn:
        deck = await conn.fetchrow(
            "SELECT * FROM decks WHERE id = $1 AND user_id = $2",
            deck_id,
            user_id,
        )
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")

        rows = await conn.fetch(
            f"""
            SELECT c.*, p.title AS paper_title, p.authors AS paper_authors
            FROM cards c
            LEFT JOIN papers p ON p.id = c.paper_id
            WHERE c.deck_id = $1 AND c.user_id = $2
              AND {CURRENT_CARD_SQL}
            ORDER BY c.created_at
            """,
            deck_id,
            user_id,
        )

    if not rows:
        raise HTTPException(status_code=400, detail="Deck has no cards to export")

    cards_for_export: list[dict] = []
    for row in rows:
        evidence = row["evidence"] or {}
        evidence_parts: list[str] = []
        if evidence.get("quote"):
            evidence_parts.append(f'"{evidence["quote"]}"')
        if evidence.get("page_number"):
            evidence_parts.append(f"(p. {evidence['page_number']})")

        source_parts: list[str] = []
        if row["paper_title"]:
            source_parts.append(row["paper_title"])
        if row["paper_authors"]:
            source_parts.append(", ".join(row["paper_authors"][:3]))

        cards_for_export.append(
            {
                "front": row["front"],
                "back": row["back"],
                "source": " — ".join(source_parts) if source_parts else "",
                "evidence_text": " ".join(evidence_parts) if evidence_parts else "",
            }
        )

    apkg_bytes = anki_exporter.export_deck(deck["name"], cards_for_export)

    safe_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", deck["name"])[:100]

    return StreamingResponse(
        io.BytesIO(apkg_bytes),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.apkg"'},
    )
