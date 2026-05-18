"""Back-compat regression for the PEP 562 lazy models barrel (A2).

Asserts that every representative symbol accessible via ``paper_ingestion.models``
is *identical* (``is``) to the same symbol exported by its owning submodule.
Proves the lazy __getattr__ barrel resolves provenance correctly and that no
eager import was accidentally removed.
"""

from __future__ import annotations

import importlib

import pytest


# ---------------------------------------------------------------------------
# Representative sample: one symbol per submodule.
# Format: (barrel_name, owning_submodule)
# ---------------------------------------------------------------------------
SAMPLE: list[tuple[str, str]] = [
    # authors
    ("TrackedAuthorCreate", "paper_ingestion.models.authors"),
    ("AuthorCheckResponse", "paper_ingestion.models.authors"),
    # contradictions
    ("ContradictionScanRequest", "paper_ingestion.models.contradictions"),
    # dashboard
    ("DashboardMetrics", "paper_ingestion.models.dashboard"),
    # extractions
    ("ExtractionField", "paper_ingestion.models.extractions"),
    ("VerificationReport", "paper_ingestion.models.extractions"),
    # kg
    ("CitationRelation", "paper_ingestion.models.kg"),
    ("GraphNode", "paper_ingestion.models.kg"),
    # notes
    ("NoteCreate", "paper_ingestion.models.notes"),
    # papers
    ("PaperCreate", "paper_ingestion.models.papers"),
    ("SourceType", "paper_ingestion.models.papers"),
    ("compute_priority", "paper_ingestion.models.papers"),
    ("priority_level", "paper_ingestion.models.papers"),
    # pulse
    ("PulseGenerateResponse", "paper_ingestion.models.pulse"),
    # rag
    ("AskRequest", "paper_ingestion.models.rag"),
    ("CrossPaperAskRequest", "paper_ingestion.models.rag"),
    # topics
    ("TopicCreate", "paper_ingestion.models.topics"),
    ("ConfigEntry", "paper_ingestion.models.topics"),
]


@pytest.mark.parametrize("symbol_name,owning_module", SAMPLE)
def test_barrel_symbol_is_submodule_symbol(symbol_name: str, owning_module: str) -> None:
    """Symbol from barrel must be identical to the one in the owning submodule."""
    barrel = importlib.import_module("paper_ingestion.models")
    submod = importlib.import_module(owning_module)

    from_barrel = getattr(barrel, symbol_name)
    from_submod = getattr(submod, symbol_name)

    assert from_barrel is from_submod, (
        f"paper_ingestion.models.{symbol_name} is not the same object as "
        f"{owning_module}.{symbol_name}"
    )


def test_barrel_all_contains_all_mapped_symbols() -> None:
    """Every symbol in _SYMBOL_MODULE must be in __all__."""
    import paper_ingestion.models as barrel

    missing = set(barrel._SYMBOL_MODULE) - set(barrel.__all__)
    assert not missing, f"Symbols in _SYMBOL_MODULE but missing from __all__: {missing}"


def test_barrel_all_mapped_symbols_resolvable() -> None:
    """Every name in __all__ must be resolvable via the barrel (no AttributeError)."""
    import paper_ingestion.models as barrel

    failures: list[str] = []
    for name in barrel.__all__:
        try:
            getattr(barrel, name)
        except AttributeError as exc:
            failures.append(f"{name}: {exc}")

    assert not failures, "Unresolvable __all__ entries:\n" + "\n".join(failures)


def test_barrel_unknown_attr_raises() -> None:
    """Accessing an unknown name must raise AttributeError (not hang or return None)."""
    import paper_ingestion.models as barrel

    with pytest.raises(AttributeError, match="no attribute"):
        _ = barrel._nonexistent_symbol_xyzzy  # type: ignore[attr-defined]
