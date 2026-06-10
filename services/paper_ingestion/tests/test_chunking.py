"""Offset-exactness tests for ``paper_ingestion.ingestion.chunking.chunk_text``."""

from __future__ import annotations

import tiktoken

from paper_ingestion.ingestion.chunking import chunk_text
from paper_ingestion.ingestion.embedding_config import CHUNK_TOKEN_LIMIT


def _filler_paragraph(tag: str) -> str:
    return (f"{tag} lorem ipsum dolor sit amet consectetur adipiscing elit " * 25).strip()


def test_chunk_text_start_char_offsets_exact_multi_section_multi_paragraph() -> None:
    r"""Characterization guard for falsified audit finding M11b (2026-06-10).

    The audit claimed start_char drifts across multi-paragraph/multi-section
    inputs. Reality: the paragraph sub-split ``re.split(r"\n\n(?!\$\$)")``
    consumes exactly two newline chars per boundary, so the
    ``para_offset += len(para) + 2`` accounting keeps every chunk's
    ``[start_char:end_char]`` window an exact substring of the original text
    whose ``strip()`` equals the chunk content. The proposed "fix" would have
    broken these exact offsets.
    """
    enc = tiktoken.get_encoding("cl100k_base")

    def section(name: str) -> str:
        paragraphs = [
            f"## {name}",
            _filler_paragraph(f"{name}-first"),
            f"The {name} relation is displayed below.\n\n$$E_{{{name}}} = mc^2$$",
            _filler_paragraph(f"{name}-second"),
            _filler_paragraph(f"{name}-third"),
        ]
        # Every paragraph stays under CHUNK_TOKEN_LIMIT so chunking never enters
        # the token-window force-split path — this test pins the paragraph
        # sub-split offset accounting the falsified finding targeted.
        assert all(len(enc.encode(p)) <= CHUNK_TOKEN_LIMIT for p in paragraphs)
        return "\n\n".join(paragraphs)

    sections = [section("Introduction"), "\n" + section("Methods"), "\n" + section("Results")]
    # Each section must exceed CHUNK_TOKEN_LIMIT to force the paragraph sub-split path.
    assert all(len(enc.encode(s)) > CHUNK_TOKEN_LIMIT for s in sections)
    text = "".join(sections)

    chunks = chunk_text(text, page_anchors=None, encoding=enc)

    assert len(chunks) >= 6, f"expected multiple flushes across 3 sections; got {len(chunks)}"
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        window = text[c.start_char : c.end_char]
        assert window.strip() == c.content, (
            f"chunk {c.chunk_index}: text[{c.start_char}:{c.end_char}] does not "
            f"reconstruct the chunk content — start_char/end_char drifted"
        )
