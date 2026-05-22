#!/usr/bin/env bash
# Render litellm/config.yaml's `smart` and `smart-fallback` aliases from env.
# Inputs (env vars):
#   JARVIS_LLM_BACKEND          — "vllm" | "ollama"
#   JARVIS_SMART_MODEL          — model id (e.g. "Qwen/Qwen3-8B-AWQ" or "qwen3:8b")
#   JARVIS_SMART_FALLBACK_MODEL — optional; if unset, read fallback_for_tier
#                                 from config/llm-tier-candidates.yaml for JARVIS_HW_TIER
#   JARVIS_HW_TIER              — used to resolve the fallback default

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${REPO_ROOT}/litellm/config.yaml"

: "${JARVIS_LLM_BACKEND:?must be set}"
: "${JARVIS_SMART_MODEL:?must be set}"

if [ -z "${JARVIS_SMART_FALLBACK_MODEL:-}" ]; then
  : "${JARVIS_HW_TIER:?required when JARVIS_SMART_FALLBACK_MODEL not set}"
  JARVIS_SMART_FALLBACK_MODEL=$(python3 - "${JARVIS_HW_TIER}" "${REPO_ROOT}" <<'PY'
import sys, yaml
tier, repo_root = sys.argv[1], sys.argv[2]
data = yaml.safe_load(open(f"{repo_root}/config/llm-tier-candidates.yaml"))
fb = data["tiers"][tier]["fallback_for_tier"]
print(fb["model"])
PY
)
fi

python3 - "${CONFIG}" "${JARVIS_LLM_BACKEND}" "${JARVIS_SMART_MODEL}" "${JARVIS_SMART_FALLBACK_MODEL}" <<'PY'
import os
import shutil
import sys
import tempfile

import yaml

config_path, backend, smart_model, fallback_model = sys.argv[1:5]
with open(config_path) as f:
    config = yaml.safe_load(f)


def _alias_params(b: str, m: str, timeout: int) -> dict:
    """Build litellm_params for a single (backend, model) alias.

    Must include api_base — both ollama and vllm run as sibling containers in
    docker-compose's `jarvis` network, so LiteLLM cannot fall back to
    localhost defaults (would resolve inside the LiteLLM container itself).
    For vllm the openai provider proxies to the OpenAI-compatible vLLM server.
    """
    if b == "ollama":
        return {
            "model": f"ollama/{m}",
            "api_base": "http://ollama:11434",
            "timeout": timeout,
            "num_retries": 2,
        }
    if b == "vllm":
        return {
            "model": f"openai/{m}",
            "api_base": "http://vllm:8080/v1",
            "api_key": "vllm-no-key",
            "timeout": timeout,
            "num_retries": 2,
        }
    raise SystemExit(f"unknown JARVIS_LLM_BACKEND: {b!r}")


ml = config.setdefault("model_list", [])


def upsert(name: str, params: dict) -> None:
    for entry in ml:
        if entry.get("model_name") == name:
            entry["litellm_params"] = params
            return
    ml.append({"model_name": name, "litellm_params": params})


upsert("smart", _alias_params(backend, smart_model, 60))
upsert("smart-fallback", _alias_params("ollama", fallback_model, 120))

rs = config.setdefault("router_settings", {})
fbs = rs.setdefault("fallbacks", [])
mapping = next((f for f in fbs if "smart" in f), None)
if mapping is None:
    fbs.append({"smart": ["smart-fallback"]})
else:
    mapping["smart"] = ["smart-fallback"]

fd, tmp = tempfile.mkstemp(dir=os.path.dirname(config_path), prefix=".config.yaml.")
with os.fdopen(fd, "w") as f:
    yaml.safe_dump(config, f, sort_keys=False)
shutil.move(tmp, config_path)
print(f"rendered: smart={backend}/{smart_model} fallback=ollama/{fallback_model}")
PY
