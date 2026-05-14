"""Dynamic Pydantic model factory for template-driven extraction.

Each extraction template defines its own set of field names at runtime.
We use ``pydantic.create_model`` to build a per-template response model
so that ``call_llm_structured`` can validate field values at the schema level.
"""

from functools import lru_cache
from typing import Any, Optional

from pydantic import BaseModel, Field, create_model


class ExtractedFieldOutput(BaseModel):
    value: str | int | float | None = Field(
        default=None, description="Extracted value or null if not found"
    )
    quote: str | None = Field(
        default=None, description="Verbatim quote — exact substring of source text"
    )


@lru_cache(maxsize=128)
def _build_extraction_response_model(field_names: tuple[str, ...]) -> type[BaseModel]:
    """Build (and cache) a Pydantic model for the given ordered tuple of field names.

    The cache key is an ordered tuple so that the same set of fields always
    returns the same class object — avoids re-compiling Pydantic validators
    on every extraction call.
    """
    fields_kwargs: dict[str, Any] = {
        # pydantic create_model requires Optional[X] here; the PEP 604 X|None
        # form doesn't round-trip through dynamic model construction.
        name: (Optional[ExtractedFieldOutput], None)  # noqa: UP007,UP045
        for name in field_names
    }
    return create_model(  # type: ignore[call-overload]
        f"ExtractionOutput_{abs(hash(field_names)) & 0xFFFF:04x}",
        **fields_kwargs,
    )
