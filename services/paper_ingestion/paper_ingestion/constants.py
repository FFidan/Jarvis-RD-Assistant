"""Package-level constants for paper_ingestion.

Leaf module: imports only stdlib so it can be safely imported by any
module in the package without risk of circular imports.
"""

# Default Ollama model tags for the two LLM roles. Both routers/system.py
# (_ROLE_CODE_DEFAULTS) and main.py (_LITELLM_ROLE_FALLBACKS) reference
# these so a tag change is made in exactly one place.
SMART_MODEL_DEFAULT = "qwen3:8b"
FAST_MODEL_DEFAULT = "qwen3:4b"
