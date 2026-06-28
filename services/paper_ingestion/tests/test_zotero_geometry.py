"""Unit tests for the pure Zotero export geometry (zotero_geometry.py).

These are the load-bearing inverse-transform tests: stored normalized,
top-origin highlight rects → Zotero bottom-origin ``annotationPosition`` rects.
No I/O, no Zotero, no pypdfium2.
"""

from __future__ import annotations

import re

from paper_ingestion.integrations.zotero_geometry import (
    build_sort_index,
    denormalize_rect_to_zotero,
)


def _rect(x0: float, y0: float, x1: float, y1: float) -> dict:
    """Stored-rect JSONB shape with a single per-line rectangle."""
    return {
        "boundingRect": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
        "rects": [{"x0": x0, "y0": y0, "x1": x1, "y1": y1}],
    }


def test_denormalize_worked_example():
    """The Section-3 worked example: US-Letter page 3 → exact integer-point rect."""
    rect = _rect(0.1176, 0.1098, 0.4902, 0.1287)
    out = denormalize_rect_to_zotero(rect, 612, 792)
    assert out == [[72.0, 690.0, 300.0, 705.0]]


def test_denormalize_y_flip_ordering_invariant():
    """The emitted bottom-origin rect always has bottom < top (y1 < y2) and left < right."""
    cases = [
        (_rect(0.10, 0.10, 0.50, 0.13), 612, 792),
        (_rect(0.05, 0.20, 0.95, 0.40), 595, 842),  # A4
        (_rect(0.0, 0.0, 1.0, 0.02), 1000, 1000),
        (_rect(0.30, 0.50, 0.70, 0.55), 200, 1400),
    ]
    for rect, width, height in cases:
        (line,) = denormalize_rect_to_zotero(rect, width, height)
        left, bottom, right, top = line
        assert bottom < top, f"expected bottom < top (bottom-origin), got {line}"
        assert left < right, f"expected left < right, got {line}"


def test_denormalize_multiline_one_rect_per_line():
    """A multi-line highlight emits one Zotero rect per stored per-line rect."""
    rect = {
        "boundingRect": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2},
        "rects": [
            {"x0": 0.1, "y0": 0.10, "x1": 0.9, "y1": 0.12},
            {"x0": 0.1, "y0": 0.15, "x1": 0.5, "y1": 0.17},
        ],
    }
    out = denormalize_rect_to_zotero(rect, 612, 792)
    assert len(out) == 2
    # Each line obeys the bottom < top ordering.
    for left, bottom, right, top in out:
        assert bottom < top
        assert left < right


def test_denormalize_bounds_scale_with_page_size():
    """A full-width, top-aligned rect maps to the page edges in PDF points."""
    rect = _rect(0.0, 0.0, 1.0, 0.10)
    (line,) = denormalize_rect_to_zotero(rect, 612, 792)
    left, bottom, right, top = line
    assert left == 0.0
    assert right == 612.0
    assert top == 792.0  # y0=0 (page top) → bottom-origin top = H
    assert bottom == round(0.90 * 792)  # y1=0.10 → bottom-origin = (1-0.10)*H


def test_build_sort_index_worked_example():
    """build_sort_index zero-pads pageIndex|0|yTop to the documented widths."""
    assert build_sort_index(2, 705.0) == "00002|000000|00705"


def test_build_sort_index_shape_and_page_field():
    """The sort index matches ^\\d{5}\\|\\d{6}\\|\\d{5}$ and carries the right pageIndex."""
    idx = build_sort_index(7, 123.6)
    assert re.fullmatch(r"\d{5}\|\d{6}\|\d{5}", idx)
    assert idx.split("|")[0] == "00007"
    assert idx.split("|")[2] == "00124"  # round(123.6)
