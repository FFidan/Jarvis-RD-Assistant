"""Syntactic validation of model identifiers, shared across the model plane.

A model identifier travels to LiteLLM's admin API and into provider HTTP
requests, so a value like ``../../etc/passwd`` or ``; rm -rf /`` must never
survive validation. Which rule applies depends on the provider kind: routers
and self-hosted endpoints namespace their ids (``vendor/model``), every other
kind uses a single strict segment.
"""

from __future__ import annotations

import re

__all__ = [
    "NAMESPACED_PROVIDER_KINDS",
    "validate_model_name",
    "validate_namespaced_model_suffix",
]

# Provider kinds whose model ids may carry a vendor namespace: routers serve
# ``vendor/model`` and self-hosted endpoints serve the HF repo id ``org/model``
# (or a bare name the endpoint chose). Every other kind keeps the strict
# single-segment rule.
NAMESPACED_PROVIDER_KINDS = frozenset({"router", "self_hosted"})

_SEGMENT_PATTERN = re.compile(r"[a-zA-Z0-9._:\-]+")


def validate_model_name(model_name: str) -> None:
    """Reject model names that contain path traversal or shell metacharacters.

    Permit only ``[a-zA-Z0-9._:-]`` characters (covers all real Ollama and
    cloud ids), and reject names made of dots alone so neither ``.`` nor ``..``
    ever reaches a config surface.
    """
    if not _SEGMENT_PATTERN.fullmatch(model_name):
        raise ValueError(
            f"Model name {model_name!r} contains disallowed characters. "
            "Only alphanumerics and . _ : - are permitted."
        )
    if model_name.strip(".") == "":
        raise ValueError(f"Model name {model_name!r} consists only of dots.")


def validate_namespaced_model_suffix(model_suffix: str) -> None:
    """Validate a suffix carrying at most one vendor namespace: ``model`` or ``vendor/model``.

    Each segment is held to :func:`validate_model_name`'s single-segment rule,
    so the only thing this permits beyond it is ONE separator.
    """
    segments = model_suffix.split("/")
    try:
        for segment in segments:
            validate_model_name(segment)
    except ValueError as exc:
        raise ValueError(_namespaced_error(model_suffix)) from exc
    if len(segments) > 2:
        raise ValueError(_namespaced_error(model_suffix))


def _namespaced_error(model_suffix: str) -> str:
    return (
        f"Model name {model_suffix!r} is not a valid namespaced model id. "
        "These providers use at most one vendor prefix, like vendor/model-name."
    )
