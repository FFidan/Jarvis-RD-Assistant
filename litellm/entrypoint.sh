#!/bin/sh
set -eu
export LITELLM_MASTER_KEY="$(cat /run/secrets/litellm_master_key)"
exec litellm "$@"
