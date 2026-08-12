#!/usr/bin/env python3
"""Exercise the runtime capabilities an image import check cannot reach.

The published-image gate imports each service entrypoint, which proves the
dependency set resolves. It does not prove that lazily imported native stacks
load: the document pipeline builds its converter on first use, so a mismatched
compiled dependency stays invisible until a researcher converts a real PDF.
This runs inside the exact verification digest and fails the release instead.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path


def _extract(pdf_path: Path) -> str:
    """Extract text using the service's own synchronous conversion entry point.

    Imported here rather than at module scope so the native-vision check still
    runs in an environment that carries only the compiled stack.
    """
    from paper_ingestion.pdf_processor import _extract_text_sync

    text, _page_anchors = _extract_text_sync(pdf_path)
    return text


def check_native_vision() -> None:
    """Load the compiled vision stack and call a registered custom operator.

    ``import torchvision`` registers operators against the loaded torch build;
    a mismatched pair raises here rather than at the first conversion.
    """
    import torch
    import torchvision
    from torchvision.ops import nms

    boxes = torch.tensor([[0.0, 0.0, 1.0, 1.0], [0.0, 0.0, 1.0, 1.0]])
    scores = torch.tensor([0.9, 0.8])
    kept = nms(boxes, scores, 0.5)
    if kept.numel() != 1:
        raise SystemExit(f"nms returned {kept.numel()} boxes, expected 1")
    print(f"native vision ok: torch {torch.__version__}, torchvision {torchvision.__version__}")


def check_pdf_conversion(fixture: Path) -> None:
    """Run the real document pipeline over a known-good PDF.

    The fixture is mounted from the repository rather than generated: a
    synthetic blank page carries no text, so asserting on extracted text would
    pass whether or not extraction actually worked.
    """
    text = _extract(fixture)
    if not text.strip():
        raise SystemExit("document conversion produced no text")
    print(f"pdf conversion ok: {len(text)} characters")


def check_malformed_pdf_rejected() -> None:
    """A file that is not a PDF must be refused, not silently converted."""
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "broken.pdf"
        source.write_bytes(b"this is not a pdf")
        from docling.exceptions import ConversionError
        from pypdfium2 import PdfiumError

        try:
            _extract(source)
        except (ConversionError, PdfiumError) as exc:
            # Only the document-decode failures count as a refusal. Catching any
            # exception here would let a crash, a missing file or an unloadable
            # stack read as if the pipeline had correctly rejected the input.
            print(f"malformed pdf rejected: {type(exc).__name__}")
            return
    raise SystemExit("malformed input was accepted")


# Each entry adapts the parsed command line to one check, so a check declares
# only the arguments it actually needs.
_CHECKS: dict[str, Callable[[argparse.Namespace], None]] = {
    "native-vision": lambda _args: check_native_vision(),
    "pdf": lambda args: check_pdf_conversion(args.pdf),
    "malformed-pdf": lambda _args: check_malformed_pdf_rejected(),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checks", nargs="+", choices=sorted(_CHECKS))
    parser.add_argument("--pdf", type=Path, help="PDF the pdf check converts")
    args = parser.parse_args()
    if "pdf" in args.checks and args.pdf is None:
        parser.error("--pdf is required by the pdf check")

    for name in args.checks:
        print(f"--- {name}")
        _CHECKS[name](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
