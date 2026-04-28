#!/bin/sh
set -eu
key_file="${LITELLM_MASTER_KEY_FILE:-/run/secrets/litellm_master_key}"
export LITELLM_MASTER_KEY="$(cat "$key_file")"
exec litellm "$@"
