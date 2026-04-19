"""Quote verification module (Anti-Hallucination Layer 2).

Verifies that LLM-generated quotes actually exist in the source paper text.
Uses exact string matching first, then fuzzy matching with rapidfuzz at a
97% threshold.  Implements the confidence rules from AGENTS.md:

- 100% pass  -> HIGH confidence
- >50% pass  -> MEDIUM confidence
- <=50% pass -> LOW confidence
- 0% pass    -> caller must replace summary with abstract
"""

import logging
import unicodedata

from rapidfuzz import fuzz

from app.models import (
    ChunkResponse,
    Confidence,
    KeyFinding,
    VerificationReport,
    VerificationResult,
)

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 97  # Minimum fuzz.partial_ratio score (tightened for anti-hallucination)


class QuoteVerifier:
    """Verifies LLM-generated quotes against source text.

    This is a stateless utility class.  All data is passed via method
    parameters.
    """

    def verify_quote(
        self,
        quote: str,
        full_text: str,
        chunks: list[ChunkResponse],
        _normalized_full: str | None = None,
    ) -> VerificationResult:
        """Verify a single quote against the full paper text and chunks.

        Tries exact substring match first, then fuzzy match (partial_ratio >= 97%).
        Sets ``matched_span_start`` to the byte offset of ``matched_text`` within
        ``full_text`` so callers can skip a second O(n) scan.
        """
        if not quote or not quote.strip():
            return VerificationResult(quote=quote, verified=False)

        MAX_QUOTE_LENGTH = 5000
        if len(quote) > MAX_QUOTE_LENGTH:
            logger.warning(
                "Quote too long (%d chars), truncating to %d for verification",
                len(quote),
                MAX_QUOTE_LENGTH,
            )
            quote = quote[:MAX_QUOTE_LENGTH]

        normalized_quote = self._normalize(quote)

        # --- Strategy 1: Exact substring match ---
        # Use pre-normalized if provided, otherwise normalize here
        normalized_full = (
            _normalized_full if _normalized_full is not None else self._normalize(full_text)
        )
        if normalized_quote in normalized_full:
            chunk_id, page_number = self._find_chunk_for_quote(quote, chunks)
            # B-H-01: raw find may return -1 when normalization changed whitespace/Unicode.
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
        best_chunk: ChunkResponse | None = None

        for chunk in chunks:
            score = fuzz.partial_ratio(normalized_quote, self._normalize(chunk.content))
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
        findings: list[KeyFinding],
        full_text: str,
        chunks: list[ChunkResponse],
    ) -> VerificationReport:
        """Verify all findings and compute confidence (HIGH/MEDIUM/LOW per AGENTS.md rules).

        Mutates *findings* in place (sets ``verified``, ``chunk_id``, ``page_number``).
        """
        if not findings:
            return VerificationReport(
                total_findings=0,
                verified_count=0,
                failed_count=0,
                pass_rate=0.0,
                confidence=Confidence.LOW,
                results=[],
            )

        # Pre-normalize once for all findings
        normalized_full = self._normalize(full_text)

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

        # Compute confidence per AGENTS.md rules
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

    def _find_chunk_for_quote(
        self,
        quote: str,
        chunks: list[ChunkResponse],
    ) -> tuple[int | None, int | None]:
        """Return ``(chunk_id, page_number)`` for the chunk containing *quote*, or ``(None, None)``."""
        normalized_quote = self._normalize(quote)
        for chunk in chunks:
            if normalized_quote in self._normalize(chunk.content):
                return chunk.id, chunk.page_number
        return None, None
