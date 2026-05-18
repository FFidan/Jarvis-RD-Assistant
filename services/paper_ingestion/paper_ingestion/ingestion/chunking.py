"""Markdown-aware text chunking for embedding.

Extracted verbatim from ``Embedder.chunk_text`` (C1 God-class decomposition).
The logic is byte-for-byte identical to the original method body; the only
change is that the tiktoken encoding is passed in explicitly instead of being
read from ``self._encoding``.  ``Embedder.chunk_text`` now delegates here.
"""

from __future__ import annotations

import re

from paper_ingestion.ingestion.embedding_config import (
    CHUNK_OVERLAP_TOKENS,
    CHUNK_TOKEN_LIMIT,
)
from paper_ingestion.models import ChunkForEmbedding


def chunk_text(
    text: str,
    page_boundaries: list[tuple[int, int]] | None,
    encoding,
) -> list[ChunkForEmbedding]:
    """Chunk Markdown text respecting structure and math blocks.

    Strategy:
    1. Split on section headings (## )
    2. Within sections, split on paragraph boundaries (double newline)
    3. Never split inside $$...$$ display math blocks
    4. If a unit exceeds CHUNK_TOKEN_LIMIT, sub-split at paragraph boundaries
    5. Accumulate small units until reaching target size

    Parameters
    ----------
    text : str
        Full extracted Markdown text from the PDF.
    page_boundaries : list[tuple[int, int]] | None
        List of ``(start_char, end_char)`` per page.  Index 0 corresponds
        to page 1 (1-indexed for user display).
    encoding :
        tiktoken encoding used for token counting (formerly ``self._encoding``).

    Returns
    -------
    list[ChunkForEmbedding]
        Chunks ready for embedding, with character offsets and page numbers.
    """
    enc = encoding

    def token_count(s: str) -> int:
        return len(enc.encode(s))

    def find_page(char_offset: int) -> int | None:
        if not page_boundaries:
            return None
        for page_idx, (start, end) in enumerate(page_boundaries):
            if start <= char_offset < end:
                return page_idx + 1  # 1-indexed
        return len(page_boundaries)  # last page

    # Split into sections by headings, preserving the heading with each section
    sections = re.split(r"(?=\n##\s)", text)

    chunks: list[ChunkForEmbedding] = []
    chunk_index = 0
    current_text = ""
    current_start = 0
    text_offset = 0  # track position in original text

    for section in sections:
        if not section.strip():
            text_offset += len(section)
            continue

        section_tokens = token_count(section)

        if section_tokens <= CHUNK_TOKEN_LIMIT:
            # Section fits in one chunk -- try to accumulate with current
            combined = current_text + ("\n\n" if current_text else "") + section
            if token_count(combined) <= CHUNK_TOKEN_LIMIT:
                if not current_text:
                    current_start = text_offset
                current_text = combined
            else:
                # Flush current chunk, start new
                if current_text.strip():
                    mid = current_start + len(current_text) // 2
                    chunks.append(
                        ChunkForEmbedding(
                            chunk_index=chunk_index,
                            content=current_text.strip(),
                            page_number=find_page(mid),
                            start_char=current_start,
                            end_char=current_start + len(current_text),
                        )
                    )
                    chunk_index += 1
                current_text = section
                current_start = text_offset
        else:
            # Section too large -- flush current, then sub-split
            if current_text.strip():
                mid = current_start + len(current_text) // 2
                chunks.append(
                    ChunkForEmbedding(
                        chunk_index=chunk_index,
                        content=current_text.strip(),
                        page_number=find_page(mid),
                        start_char=current_start,
                        end_char=current_start + len(current_text),
                    )
                )
                chunk_index += 1
                current_text = ""

            # Sub-split on paragraphs (double newline, but not inside $$...$$)
            paragraphs = re.split(r"\n\n(?!\$\$)", section)
            para_offset = text_offset

            for para in paragraphs:
                if not para.strip():
                    para_offset += len(para) + 2  # +2 for \n\n
                    continue
                combined = current_text + ("\n\n" if current_text else "") + para
                if token_count(combined) <= CHUNK_TOKEN_LIMIT:
                    if not current_text:
                        current_start = para_offset
                    current_text = combined
                else:
                    if current_text.strip():
                        mid = current_start + len(current_text) // 2
                        chunks.append(
                            ChunkForEmbedding(
                                chunk_index=chunk_index,
                                content=current_text.strip(),
                                page_number=find_page(mid),
                                start_char=current_start,
                                end_char=current_start + len(current_text),
                            )
                        )
                        chunk_index += 1
                    # Force-split oversized paragraphs by token windows
                    if token_count(para) > CHUNK_TOKEN_LIMIT:
                        tokens = enc.encode(para)
                        # PI-CORE-005: track char advance via decoded window lengths
                        # instead of linear-interpolation which is inaccurate when
                        # token lengths vary (e.g. multibyte chars, BPE tokens).
                        char_advance = 0
                        for j in range(0, len(tokens), CHUNK_TOKEN_LIMIT - CHUNK_OVERLAP_TOKENS):
                            window = tokens[j : j + CHUNK_TOKEN_LIMIT]
                            sub_text = enc.decode(window)
                            sub_start = para_offset + char_advance
                            mid = sub_start + len(sub_text) // 2
                            chunks.append(
                                ChunkForEmbedding(
                                    chunk_index=chunk_index,
                                    content=sub_text.strip(),
                                    page_number=find_page(mid),
                                    start_char=sub_start,
                                    end_char=sub_start + len(sub_text),
                                )
                            )
                            chunk_index += 1
                            # Advance only by the non-overlapping stride so the
                            # next window's start_char aligns with decoded text.
                            stride_end = j + CHUNK_TOKEN_LIMIT - CHUNK_OVERLAP_TOKENS
                            char_advance += len(enc.decode(tokens[j:stride_end]))
                        current_text = ""
                    else:
                        current_text = para
                        current_start = para_offset
                para_offset += len(para) + 2

        text_offset += len(section)

    # Flush remaining
    if current_text.strip():
        mid = current_start + len(current_text) // 2
        chunks.append(
            ChunkForEmbedding(
                chunk_index=chunk_index,
                content=current_text.strip(),
                page_number=find_page(mid),
                start_char=current_start,
                end_char=current_start + len(current_text),
            )
        )

    return chunks
