#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
rand() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
[ -s secrets/langfuse_init_pk.txt ] || { printf 'pk-lf-%s' "$(rand)" > secrets/langfuse_init_pk.txt; echo gen pk; }
[ -s secrets/langfuse_init_sk.txt ] || { printf 'sk-lf-%s' "$(rand)" > secrets/langfuse_init_sk.txt; echo gen sk; }
chmod 600 secrets/langfuse_init_pk.txt secrets/langfuse_init_sk.txt
