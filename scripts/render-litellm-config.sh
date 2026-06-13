#!/usr/bin/env bash
# De-seed guard for litellm/config.yaml.
#
# The switchable model aliases ("smart", "fast", "smart-fallback") live in
# LiteLLM's admin database, delivered via POST /model/new by the
# paper_ingestion service (boot reconciler + Settings model picker). They must
# NOT exist in the YAML: YAML-seeded deployments cannot be removed at runtime
# (/model/delete only deletes DB rows), so a YAML "smart" would STACK with the
# DB "smart" and latency-based routing could keep preferring the stale model.
#
# This script therefore:
#   1. REMOVES any smart/fast/smart-fallback entries from model_list
#      (upgrade path: older installs and older versions of this script seeded
#      them from JARVIS_SMART_MODEL/tier env vars).
#   2. Ensures router_settings.fallbacks maps smart -> ["smart-fallback"]
#      (the DB-created fallback deployment group) and drops legacy raw
#      provider-model fallback strings.
#
# It takes no env inputs and is idempotent. Install-time model choice reaches
# the system via .env (JARVIS_SMART_MODEL) + the boot reconciler, not via this
# file.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${REPO_ROOT}/litellm/config.yaml"

python3 - "${CONFIG}" <<'PY'
import os
import shutil
import sys
import tempfile

import yaml

SWITCHABLE_ALIASES = {"smart", "fast", "smart-fallback"}

config_path = sys.argv[1]
with open(config_path) as f:
    raw = f.read()

# Preserve the top-of-file comment block (lost by yaml.safe_dump).
header_lines: list[str] = []
for line in raw.splitlines(keepends=True):
    if line.lstrip().startswith("#") or line.strip() == "":
        header_lines.append(line)
    else:
        break
header = "".join(header_lines)

config = yaml.safe_load(raw)

changed = False

# 1. Scrub switchable aliases from model_list (keep embed/embed-4b etc.).
ml = config.get("model_list") or []
kept = [e for e in ml if e.get("model_name") not in SWITCHABLE_ALIASES]
if kept != ml:
    config["model_list"] = kept
    changed = True

# 2. Normalize router fallbacks: smart -> ["smart-fallback"], drop legacy
#    raw-model-string groups (e.g. fast -> ["ollama/qwen3:4b"]).
rs = config.setdefault("router_settings", {})
fbs = rs.get("fallbacks") or []
new_fbs = [{"smart": ["smart-fallback"]}]
if fbs != new_fbs:
    rs["fallbacks"] = new_fbs
    changed = True

if not changed:
    print("litellm config already de-seeded; nothing to do")
    raise SystemExit(0)

fd, tmp = tempfile.mkstemp(dir=os.path.dirname(config_path), prefix=".config.yaml.")
with os.fdopen(fd, "w") as f:
    f.write(header)
    yaml.safe_dump(config, f, sort_keys=False)
shutil.move(tmp, config_path)
print("de-seeded litellm config: removed switchable aliases, normalized fallbacks")
PY
