"""Dynamic Pydantic model factory for template-driven extraction.

Each extraction template defines its own set of field names at runtime.
We use ``pydantic.create_model`` to build a per-template response model
so that ``call_llm_structured`` can validate field values at the schema level.
"""

import keyword
import re
from functools import lru_cache
from typing import Any, Optional

from pydantic import BaseModel, Field, create_model

_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ExtractedFieldOutput(BaseModel):
    """Single extracted field value with a verbatim supporting quote.

    Attributes
    ----------
    value : str | int | float | None
        The extracted field value, or ``None`` when the field is absent.
    quote : str | None
        Exact verbatim quote from the source text that supports the value.
    """

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

    Raises
    ------
    ValueError
        If any name in *field_names* is not a valid Python identifier, is a
        Python keyword, is a dunder name, or starts with ``model_`` (which
        Pydantic reserves for its own methods).
    """
    for name in field_names:
        if not _FIELD_NAME_RE.match(name):
            raise ValueError(f"Invalid field name {name!r}: must match [A-Za-z_][A-Za-z0-9_]*")
        if keyword.iskeyword(name):
            raise ValueError(f"Invalid field name {name!r}: Python keyword")
        if name.startswith("__") and name.endswith("__"):
            raise ValueError(f"Invalid field name {name!r}: dunder names reserved")
        if name.startswith("_"):
            raise ValueError(
                f"Invalid field name {name!r}: underscore-prefixed names are treated as "
                "Pydantic private attributes and would be silently dropped"
            )
        if name.startswith("model_"):
            raise ValueError(f"Invalid field name {name!r}: 'model_*' names reserved by Pydantic")
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
