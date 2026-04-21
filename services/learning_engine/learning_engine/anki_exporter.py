"""Anki .apkg export via genanki.

Optional extension — converts JARVIS flashcards to Anki-importable
.apkg format. The core FSRS review path does not depend on this module.
"""

import hashlib
import io

import genanki


class AnkiExporter:
    """Export flashcard decks as Anki .apkg files."""

    # Stable model ID — generated once, must never change
    JARVIS_MODEL_ID = 1607392319

    def __init__(self):
        self.model = genanki.Model(
            self.JARVIS_MODEL_ID,
            "JARVIS Research Card",
            fields=[
                {"name": "Front"},
                {"name": "Back"},
                {"name": "Source"},
                {"name": "Evidence"},
            ],
            templates=[
                {
                    "name": "Card 1",
                    "qfmt": "{{Front}}<br><small>{{Source}}</small>",
                    "afmt": (
                        "{{FrontSide}}<hr id=answer>{{Back}}"
                        "<br><br><small><i>{{Evidence}}</i></small>"
                    ),
                }
            ],
        )

    def export_deck(self, deck_name: str, cards: list[dict]) -> bytes:
        """Export cards as an Anki .apkg file.

        Parameters
        ----------
        deck_name : str
            Name for the Anki deck.
        cards : list[dict]
            Cards with keys: front, back, source, evidence_text.

        Returns
        -------
        bytes
            The .apkg file content.
        """
        deck_id = int(
            hashlib.sha1(deck_name.encode(), usedforsecurity=False).hexdigest(),
            16,
        ) % (1 << 53)
        deck = genanki.Deck(deck_id, deck_name)

        for card in cards:
            note = genanki.Note(
                model=self.model,
                fields=[
                    card["front"],
                    card["back"],
                    card.get("source", ""),
                    card.get("evidence_text", ""),
                ],
            )
            deck.add_note(note)

        buf = io.BytesIO()
        genanki.Package(deck).write_to_file(buf)
        buf.seek(0)
        return buf.getvalue()
