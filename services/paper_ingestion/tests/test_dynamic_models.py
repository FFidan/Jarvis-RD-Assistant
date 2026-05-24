"""Tests for _build_extraction_response_model field-name validation.

Covers:
- Six categories of invalid field names that must raise ValueError.
- Valid field names that must pass and produce a usable Pydantic model.
- lru_cache behaviour: a cached call with valid names is not re-validated
  on repeat invocations (structural check via cache_info).
"""

import pytest

from paper_ingestion.extraction.dynamic_models import _build_extraction_response_model


@pytest.mark.parametrize(
    "bad_name",
    [
        "123starts_with_digit",  # non-identifier: starts with digit
        "has-hyphen",  # non-identifier: contains hyphen
        "has space",  # non-identifier: contains space
        "__dunder__",  # dunder: startswith("__") and endswith("__")
        "model_validate",  # Pydantic-reserved: startswith("model_")
        "class",  # Python keyword
        "_private",  # Pydantic v2 treats leading-underscore names as private attrs (silently dropped)
    ],
)
def test_invalid_field_names_rejected(bad_name: str) -> None:
    with pytest.raises(ValueError, match="Invalid field name"):
        _build_extraction_response_model((bad_name,))


@pytest.mark.parametrize(
    "good_name",
    [
        "contribution",
        "method_used",
        "year_2024",
        "CamelCase",
        "x",
    ],
)
def test_valid_field_names_pass(good_name: str) -> None:
    model_cls = _build_extraction_response_model((good_name,))
    assert hasattr(model_cls, "model_fields")
    assert good_name in model_cls.model_fields


def test_multiple_valid_fields_produce_correct_model() -> None:
    names = ("contribution", "method_used", "year_2024")
    model_cls = _build_extraction_response_model(names)
    for name in names:
        assert name in model_cls.model_fields


def test_lru_cache_is_present_and_used() -> None:
    """Same tuple of valid names must return the identical class object (cached)."""
    names = ("alpha", "beta")
    cls_first = _build_extraction_response_model(names)
    cls_second = _build_extraction_response_model(names)
    assert cls_first is cls_second


def test_different_field_tuples_produce_distinct_models() -> None:
    cls_a = _build_extraction_response_model(("field_a",))
    cls_b = _build_extraction_response_model(("field_b",))
    assert cls_a is not cls_b
    assert "field_a" in cls_a.model_fields
    assert "field_b" in cls_b.model_fields


def test_model_word_without_underscore_suffix_is_allowed() -> None:
    """'modelling' starts with 'model' but NOT 'model_' — must be accepted."""
    model_cls = _build_extraction_response_model(("modelling",))
    assert "modelling" in model_cls.model_fields


def test_model_star_prefix_variations_rejected() -> None:
    """All 'model_*' names must be rejected regardless of suffix."""
    for bad in ("model_", "model_dump", "model_fields", "model_config"):
        with pytest.raises(ValueError, match="Invalid field name"):
            _build_extraction_response_model((bad,))
