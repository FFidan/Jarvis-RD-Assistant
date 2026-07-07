"""Quote verification module (Anti-Hallucination Layer 2).

Verifies that LLM-generated quotes actually exist in the source paper text.
Uses exact string matching first, then fuzzy matching with rapidfuzz at a
97% threshold.  Implements the confidence rules:

- 100% pass  -> HIGH confidence
- >50% pass  -> MEDIUM confidence
- <=50% pass -> LOW confidence
- 0% pass    -> caller must replace summary with abstract

Canonical location — ``paper_ingestion.extraction.verify`` is now a
re-export shim pointing here.
"""

import logging
import unicodedata
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

__all__ = [
    "FUZZY_THRESHOLD",
    "Confidence",
    "ChunkLike",
    "DictChunk",
    "VerificationResult",
    "VerificationReport",
    "QuoteVerifier",
]

FUZZY_THRESHOLD = 97  # Minimum fuzz.partial_ratio score (tightened for anti-hallucination)


class Confidence(StrEnum):
    """Summary confidence level based on quote verification pass rate.

    NONE = no findings to verify, HIGH = 100% verified, MEDIUM = >50%, LOW = <=50%.
    """

    NONE = "NONE"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@runtime_checkable
class ChunkLike(Protocol):
    """Structural interface for chunk objects accepted by QuoteVerifier.

    Both ``paper_ingestion.models.ChunkResponse`` (Pydantic) and any
    dataclass/NamedTuple with matching attributes satisfy this Protocol.
    """

    id: int
    content: str
    page_number: int | None


class DictChunk:
    """Lightweight wrapper that adapts a plain ``dict`` to the ``ChunkLike`` Protocol.

    Use when you have chunk data as ``{"id": ..., "content": ..., "page_number": ...}``
    dicts (e.g. from the learning-engine) and need to pass them to ``QuoteVerifier``.

    Example::

        from jarvis_common.verify import DictChunk, QuoteVerifier

        chunk_objects = [DictChunk(c) for c in raw_chunks]
        result = QuoteVerifier().verify_quote(quote, full_text, chunk_objects)
    """

    __slots__ = ("id", "content", "page_number")

    def __init__(self, data: dict) -> None:
        """Unpack ``id``, ``content``, and ``page_number`` from a raw chunk dict."""
        self.id: int = data["id"]
        self.content: str = data["content"]
        self.page_number: int | None = data.get("page_number")


class VerificationResult(BaseModel):
    """Result of verifying a single quote against source text."""

    quote: str
    verified: bool
    match_type: str | None = None  # "exact" | "fuzzy" | None
    match_score: float | None = None  # 0.0-1.0, only for fuzzy
    matched_text: str | None = None  # actual text that matched
    chunk_id: int | None = None
    page_number: int | None = None
    matched_span_start: int | None = None  # byte offset of matched_text in full_text (O(1) lookup)


class VerificationReport(BaseModel):
    """Aggregate verification results for a full summary."""

    total_findings: int
    verified_count: int
    failed_count: int
    pass_rate: float  # verified_count / total_findings
    confidence: Confidence
    results: list[VerificationResult]


class QuoteVerifier:
    """Verifies LLM-generated quotes against source text.

    This is a stateless utility class.  All data is passed via method
    parameters.  Accepts any object satisfying ``ChunkLike`` — both
    Pydantic ``ChunkResponse`` instances and plain dataclasses work.
    """

    def verify_quote(
        self,
        quote: str,
        full_text: str,
        chunks: list[ChunkLike],
        _normalized_full: str | None = None,
    ) -> VerificationResult:
        """Verify a single quote against the full paper text and chunks.

        Tries exact substring match first, then fuzzy match (partial_ratio >= 97%).
        Sets ``matched_span_start`` to the byte offset of ``matched_text`` within
        ``full_text`` so callers can skip a second O(n) scan.
        """
        if not quote or not quote.strip():
            return VerificationResult(quote=quote, verified=False)

        max_quote_length = 5000
        if len(quote) > max_quote_length:
            logger.warning(
                "Quote too long (%d chars), truncating to %d for verification",
                len(quote),
                max_quote_length,
            )
            quote = quote[:max_quote_length]

        normalized_quote = self._normalize_for_match(quote)

        # --- Strategy 1: Exact substring match ---
        # Use pre-normalized if provided, otherwise normalize here
        normalized_full = _normalized_full
        if normalized_full is None:
            normalized_full = self._normalize_for_match(full_text)
        if normalized_quote in normalized_full:
            chunk_id, page_number = self._find_chunk_for_quote(quote, chunks)
            # Raw find may return -1 when normalization changed whitespace/Unicode.
            # Accept matched_span_start=None and rely on page_number from _find_chunk_for_quote.
            span_start = full_text.find(quote)
            return VerificationResult(
                quote=quote,
                verified=True,
                match_type="exact",
                match_score=1.0,
                matched_text=quote,
                chunk_id=chunk_id,
                page_number=page_number,
                matched_span_start=span_start if span_start != -1 else None,
            )

        # --- Strategy 2: Fuzzy match against each chunk ---
        best_score = 0.0
        best_chunk: ChunkLike | None = None

        for chunk in chunks:
            score = fuzz.partial_ratio(normalized_quote, self._normalize_for_match(chunk.content))
            if score > best_score:
                best_score = score
                best_chunk = chunk
                if best_score == 100:  # Perfect match — no need to check remaining
                    break

        if best_score >= FUZZY_THRESHOLD and best_chunk is not None:
            span_start = full_text.find(best_chunk.content)
            return VerificationResult(
                quote=quote,
                verified=True,
                match_type="fuzzy",
                match_score=best_score / 100.0,
                matched_text=best_chunk.content,
                chunk_id=best_chunk.id,
                page_number=best_chunk.page_number,
                matched_span_start=span_start if span_start != -1 else None,
            )

        # --- No match found ---
        logger.warning(
            "Quote verification failed (best score: %.1f%%): %.80s...", best_score, quote
        )
        return VerificationResult(
            quote=quote,
            verified=False,
            match_type=None,
            match_score=best_score / 100.0 if best_score > 0 else None,
        )

    def verify_findings(
        self,
        findings: list,
        full_text: str,
        chunks: list[ChunkLike],
    ) -> VerificationReport:
        """Verify findings and compute confidence (HIGH/MEDIUM/LOW per anti-hallucination rules).

        Mutates *findings* in place (sets ``verified``, ``chunk_id``, ``page_number``).
        Each element of *findings* must have a ``.quote`` attribute and mutable
        ``.verified``, ``.chunk_id``, ``.page_number`` attributes.
        """
        if not findings:
            return VerificationReport(
                total_findings=0,
                verified_count=0,
                failed_count=0,
                pass_rate=0.0,
                confidence=Confidence.NONE,
                results=[],
            )

        # Pre-normalize once for all findings
        normalized_full = self._normalize_for_match(full_text)

        results: list[VerificationResult] = []
        for finding in findings:
            result = self.verify_quote(
                finding.quote, full_text, chunks, _normalized_full=normalized_full
            )
            results.append(result)
            # NOTE: Mutates findings in place — caller depends on this behavior
            finding.verified = result.verified
            if result.verified:
                finding.chunk_id = result.chunk_id
                finding.page_number = result.page_number
            else:
                finding.chunk_id = None
                finding.page_number = None

        verified_count = sum(1 for r in results if r.verified)
        total = len(findings)
        failed_count = total - verified_count
        pass_rate = verified_count / total

        # Compute confidence per the anti-hallucination rules
        if pass_rate == 1.0:
            confidence = Confidence.HIGH
        elif pass_rate > 0.5:
            confidence = Confidence.MEDIUM
        else:
            confidence = Confidence.LOW

        return VerificationReport(
            total_findings=total,
            verified_count=verified_count,
            failed_count=failed_count,
            pass_rate=pass_rate,
            confidence=confidence,
            results=results,
        )

    @staticmethod
    def _normalize(text: str) -> str:
        """NFKD decomposition, lowercase, collapse whitespace."""
        text = unicodedata.normalize("NFKD", text)
        return " ".join(text.lower().split())

    @staticmethod
    def _strip_surrounding_quote_wrappers(text: str) -> str:
        """Remove balanced outer quote wrappers without touching inner punctuation."""
        stripped = text.strip()
        quote_pairs = (
            ('"', '"'),
            ("'", "'"),
            ("\u201c", "\u201d"),
            ("\u2018", "\u2019"),
            ("\u00ab", "\u00bb"),
            ("\u2039", "\u203a"),
        )
        changed = True
        while changed and len(stripped) >= 2:
            changed = False
            for left, right in quote_pairs:
                if stripped.startswith(left) and stripped.endswith(right):
                    stripped = stripped[1:-1].strip()
                    changed = True
                    break
        return stripped

    @classmethod
    def _normalize_for_match(cls, text: str) -> str:
        """Normalize deterministic quote-formatting noise for matching only."""
        text = cls._strip_surrounding_quote_wrappers(text)
        text = text.translate(
            str.maketrans(
                {
                    "\u2018": "'",
                    "\u2019": "'",
                    "\u201a": "'",
                    "\u201b": "'",
                    "\u201c": '"',
                    "\u201d": '"',
                    "\u201e": '"',
                    "\u201f": '"',
                    "\u2010": "-",
                    "\u2011": "-",
                    "\u2012": "-",
                    "\u2013": "-",
                    "\u2014": "-",
                    "\u2212": "-",
                }
            )
        )
        return cls._normalize(text)

    def _find_chunk_for_quote(
        self,
        quote: str,
        chunks: list[ChunkLike],
    ) -> tuple[int | None, int | None]:
        """Return ``(chunk_id, page_number)`` for the chunk containing *quote*.

        Returns ``(None, None)`` if no chunk contains the quote.
        """
        normalized_quote = self._normalize_for_match(quote)
        for chunk in chunks:
            if normalized_quote in self._normalize_for_match(chunk.content):
                return chunk.id, chunk.page_number
        return None, None
