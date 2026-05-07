"""Deprecated location — use ``jarvis_common.verify`` directly.

This module is now a re-export shim.  All logic lives in
``jarvis_common.verify``; this file exists only because 20+ call sites
inside ``paper_ingestion`` still import from this path.

.. deprecated::
    Import from ``jarvis_common.verify`` instead.  This shim will be
    removed once all callers have been migrated.
"""

from jarvis_common import verify as _verify

FUZZY_THRESHOLD = _verify.FUZZY_THRESHOLD
QuoteVerifier = _verify.QuoteVerifier

__all__ = ["FUZZY_THRESHOLD", "QuoteVerifier"]
