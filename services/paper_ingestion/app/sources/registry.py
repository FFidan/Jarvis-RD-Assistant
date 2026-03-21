"""Source plugin registry.

Provides a ``@register_source`` decorator and lookup functions that map
``source_type`` strings to ``PaperSource`` subclasses.  The registry is
populated at import time; enabled/disabled state comes from the DB at
runtime.
"""

from typing import Type, TypeVar

from app.sources.base import PaperSource

_T = TypeVar("_T", bound=PaperSource)

# Registry populated at import time by @register_source decorators.
# Not frozen because registration happens during module import.
_SOURCE_CLASSES: dict[str, Type[PaperSource]] = {}


def register_source(cls: Type[_T]) -> Type[_T]:
    """Class decorator that registers a PaperSource implementation.

    Parameters
    ----------
    cls : Type[PaperSource]
        The source class to register. Must have a ``source_type`` class variable.

    Returns
    -------
    Type[_T]
        The unmodified class (registration-only decorator).

    Raises
    ------
    ValueError
        If ``source_type`` is missing or already registered.
    """
    source_type = getattr(cls, "source_type", None)
    if not source_type:
        raise ValueError(f"{cls.__name__} must define a 'source_type' class variable")
    if source_type in _SOURCE_CLASSES:
        raise ValueError(f"Source type '{source_type}' is already registered")
    _SOURCE_CLASSES[source_type] = cls
    return cls


def get_source_class(source_type: str) -> Type[PaperSource] | None:
    """Look up a registered PaperSource class by type string.

    Parameters
    ----------
    source_type : str
        The source type identifier (e.g., ``"arxiv"``).

    Returns
    -------
    Type[PaperSource] | None
        The class if registered, None otherwise.
    """
    return _SOURCE_CLASSES.get(source_type)


def get_all_source_types() -> list[str]:
    """Return all registered source type strings.

    Returns
    -------
    list[str]
        List of registered ``source_type`` values.
    """
    return list(_SOURCE_CLASSES.keys())
