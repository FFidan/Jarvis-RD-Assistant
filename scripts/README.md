# Script catalog

Retained executable scripts have one operational purpose and a supported caller.

## Operator

| Script | Purpose | Supported caller |
| --- | --- | --- |
| `backup-lifecycle.sh` | Backup lifecycle helpers. | `jarvis-research` and lifecycle tests |
| `backup.sh` | Create and inspect encrypted backup sets. | Compose backup service and `jarvis-research` |
| `jarvis-research.sh` | Managed-install lifecycle command. | Installed launcher and operator |
| `prune.sh` | Apply confirmed backup retention or deletion. | Compose backup service |
| `restore.sh` | Run the bounded restore sequence. | Transient Compose restore job |
| `rotate_config_key.py` | Re-encrypt configuration after a key rotation. | Operator maintenance procedure |
| `uninstall.sh` | Perform typed-confirmation teardown tiers. | `jarvis-research` |
| `validate-hardware.sh` | Certify a contributor host against the supported model path. | Contributor operator |

## Install and lifecycle

| Script | Purpose | Supported caller |
| --- | --- | --- |
| `export-service-requirements.sh` | Generate service dependency requirement files. | Make and contributor workflow |
| `gen-langfuse-keys.sh` | Create the optional observability keypair once. | `setup.sh` and Make |
| `init-mkcert.sh` | Create locally trusted development certificates. | `setup.sh` and Make |
| `init-secrets.sh` | Create or preserve deployment secret files. | `setup.sh` and `update.sh` |
| `jarvis-setup.sh` | Tested deprecated forwarder to the primary installer. | Older local-development automation |
| `production-readiness-check.sh` | Check host security and production prerequisites. | `setup.sh` and operator |
| `render-litellm-config.sh` | Render LiteLLM configuration from managed settings. | `setup.sh` |
| `update-bootstrap.sh` | Bridge supported older installations to the managed updater. | Operator recovery procedure |

## Recovery and security

| Script | Purpose | Supported caller |
| --- | --- | --- |
| `check-burned-secrets.sh` | Reject reused optional-observability key material. | Make and CI |
| `check-no-tracked-secrets.sh` | Reject tracked secret files. | Make and CI |
| `check-no-unsafe-resolver.py` | Check authenticated route resolver safety. | Make and CI |
| `check-python-deps.sh` | Check generated dependency parity. | Make and CI |
| `first-run-smoke.sh` | Exercise a clean-install lifecycle. | CI smoke workflow |
| `scripts/tests/test_restore_roundtrip.sh` | Test recovery through a disposable restore round trip. | Make and release verification |
| `scripts/tests/test_restore_swap_recovery.sh` | Test restore swap failure safety. | Make |
| `scripts/tests/test_update_bootstrap.sh` | Test the older-install update bridge. | Make |

## CI and checks

| Script | Purpose | Supported caller |
| --- | --- | --- |
| `check-migrations-no-tx.sh` | Reject unsupported outer migration transactions. | Make and CI |
| `check-model-catalog-freshness.py` | Check model-catalog freshness. | Nightly CI |
| `check-no-service-imports-in-common.sh` | Preserve shared-library service import boundaries. | Make and CI |
| `check-test-shape.py` | Enforce Python test-shape rules. | Make and CI |
| `ci-smoke.sh` | Run the bounded CI smoke sequence. | Make |
| `scripts/tests/test_backup_coverage.sh` | Check backup coverage assertions. | Make |
| `scripts/tests/test_corpus_visibility_qdrant.sh` | Check source-aware vector visibility. | Make and nightly CI |
| `scripts/tests/test_jarvis_research_cli.sh` | Check lifecycle command behavior. | Make |

## Performance and evaluation

| Script | Purpose | Supported caller |
| --- | --- | --- |
| `perf/gpu_probe.sh` | Capture GPU and VRAM run metadata. | `profile.sh` |
| `perf/loadgen.sh` | Drive concurrent performance load. | `profile.sh` and its test |
| `profile.sh` | Run the local profiling harness. | Make and contributor operator |
