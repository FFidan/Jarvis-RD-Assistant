"""System capabilities endpoint: GET /api/system/capabilities."""

import importlib.util

from fastapi import APIRouter, Depends, Request
from jarvis_common import require_admin_or_api_key
from jarvis_common.app_factory import STRUCTURED_DECODING_MODE
from pydantic import BaseModel

from paper_ingestion.deps import limiter

router = APIRouter(prefix="/api/system", tags=["system"])

_GRAMMAR_ENFORCING_MODES = frozenset({"JSON_SCHEMA"})


class SystemCapabilities(BaseModel):
    """Available optional heavy-library capabilities on the backend."""

    networkx: bool
    scikit_learn: bool
    structured_output_enforced: bool


@router.get(
    "/capabilities",
    response_model=SystemCapabilities,
    dependencies=[Depends(require_admin_or_api_key)],
)
@limiter.limit("30/minute")
async def get_system_capabilities(request: Request) -> SystemCapabilities:
    """Return whether optional heavy libraries (networkx, scikit-learn) are importable.

    Uses ``importlib.util.find_spec`` — no actual import, trivially cheap.
    The frontend Pulse settings UI uses this to suppress false-alarm warnings.

    ``structured_output_enforced`` reports whether the configured instructor mode
    constrains decoding to the schema grammar — derived from the module constant,
    not a live probe — so an operator-visible flip catches a silent revert to a
    prompt-only mode.
    """
    return SystemCapabilities(
        networkx=importlib.util.find_spec("networkx") is not None,
        scikit_learn=importlib.util.find_spec("sklearn") is not None,
        structured_output_enforced=STRUCTURED_DECODING_MODE in _GRAMMAR_ENFORCING_MODES,
    )
