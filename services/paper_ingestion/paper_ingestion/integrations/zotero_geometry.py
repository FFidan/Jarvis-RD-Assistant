"""Pure geometry helpers for exporting in-app highlights to Zotero.

No I/O, no Zotero, no pypdfium2 — just the coordinate algebra, so the
load-bearing inverse transform is fully unit-testable in isolation.

Stored ``paper_highlights.rect`` rectangles are normalized to ``[0, 1]`` and
**top-origin** (the forward producer is ``pdf_processor._union_to_rect``).
Zotero's ``annotationPosition`` uses **bottom-origin** PDF points
(``[left, bottom, right, top]``, y increasing upward). Export is therefore the
algebraic inverse of the stored form, including a y-flip back to bottom-origin.
"""

from __future__ import annotations

from typing import Any


def denormalize_rect_to_zotero(
    rect: dict[str, Any],
    width: float,
    height: float,
) -> list[list[float]]:
    """Convert a stored highlight ``rect`` to Zotero ``annotationPosition`` rects.

    ``rect`` is the JSONB shape ``{"boundingRect": {...}, "rects": [{x0,y0,x1,y1}, ...]}``
    with each coordinate normalized to ``[0, 1]`` and top-origin. Returns one
    ``[left, bottom, right, top]`` array (PDF points, bottom-origin) per stored
    per-line rectangle, so a multi-line highlight anchors line-by-line.

    The y-flip back to bottom-origin (``top = (1 - y0) * H``) is the inverse of
    ``_union_to_rect``'s ``y0 = (H - top) / H``. Because top-origin guarantees
    ``y0 < y1``, the emitted rect always has ``bottom < top`` (correct
    bottom-origin ordering). Coordinates are rounded to whole PDF points to keep
    the wire payload noise-free: for the common integer-point source PDF this
    recovers the original points exactly, while a PDF whose glyph boxes fall on
    sub-point boundaries is quantized to the nearest point (cosmetic, sub-pixel).

    Falls back to the single ``boundingRect`` when per-line ``rects`` is empty,
    so a highlight never decodes to an empty ``annotationPosition``.
    """
    lines = rect.get("rects") or []
    if not lines:
        bounding = rect.get("boundingRect")
        lines = [bounding] if bounding else []
    out: list[list[float]] = []
    for line in lines:
        x0 = float(line["x0"])
        y0 = float(line["y0"])
        x1 = float(line["x1"])
        y1 = float(line["y1"])
        left = round(x0 * width)
        right = round(x1 * width)
        top = round((1.0 - y0) * height)  # larger bottom-origin y (upper edge)
        bottom = round((1.0 - y1) * height)  # smaller bottom-origin y (lower edge)
        out.append([float(left), float(bottom), float(right), float(top)])
    return out


def build_sort_index(page_index: int, y_top: float) -> str:
    """Build a Zotero ``annotationSortIndex`` string for sidebar ordering.

    Pipe-delimited zero-padded triple ``"%05d|%06d|%05d"`` =
    ``pageIndex | in-page text offset | yTop``. The middle field is ``0`` (no
    text offset is available for a spatial highlight); ``yTop`` is the bounding
    rect's top edge in integer PDF points. ``page_index`` is 0-based. This index
    only affects sidebar SORT ORDER, never where the annotation renders (that is
    ``annotationPosition``), so the approximation is cosmetically sufficient.
    """
    return f"{page_index:05d}|{0:06d}|{int(round(y_top)):05d}"
