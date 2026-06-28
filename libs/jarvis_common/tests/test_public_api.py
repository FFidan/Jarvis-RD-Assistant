"""Tests for jarvis_common public API (__all__) hygiene.

Ensures private / internal symbols are not re-exported via the top-level
package, which would couple callers to implementation details.
"""

import jarvis_common


def test_langfuse_lifespan_hook_not_in_all() -> None:
    """_langfuse_lifespan_hook is an internal hook; must not be in __all__.

    Policy: private symbols prefixed with '_' must not be
    advertised in the public API. app_factory imports it directly from
    jarvis_common.llm_client, not via the jarvis_common top-level package.
    """
    assert "_langfuse_lifespan_hook" not in jarvis_common.__all__
