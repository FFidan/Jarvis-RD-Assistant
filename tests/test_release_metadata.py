"""Release metadata, shared CI entry points, and hosted-runner invariants."""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_CI_IMAGE = (
    "postgres:16.8@sha256:301bcb60b8a3ee4ab7e147932723e3abd1cef53516ce5210b39fd9fe5e3602ae"
)
QDRANT_CI_IMAGE = (
    "qdrant/qdrant:v1.13.2@sha256:81bdf0a9deedbeec68eed207145ade0b9d5db15e2f84069180711aa9698445b1"
)


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_changelog_records_the_latest_releases() -> None:
    changelog = _read("CHANGELOG.md")

    assert changelog.count("## v1.2.3 (2026-08-02)") == 1
    assert "## v1.2.2 (2026-07-31)" in changelog
    assert "## v1.2.1 (2026-07-24)" in changelog
    assert "## v1.2.0 (2026-07-23)" in changelog
    assert changelog.index("## v1.2.3") < changelog.index("## v1.2.2")
    assert changelog.index("## v1.2.2") < changelog.index("## v1.2.1")
    assert changelog.index("## v1.2.1") < changelog.index("## v1.2.0")
    assert changelog.index("## v1.2.0") < changelog.index("## v1.1.3")


def test_roadmap_lists_the_export_slice_only_once() -> None:
    roadmap = _read("ROADMAP.md")
    shipped = roadmap.split("## Shipped", 1)[1].split("## In progress", 1)[0]
    planned = roadmap.split("## Planned", 1)[1]
    shipped_words = " ".join(shipped.split())

    assert "per-paper Markdown knowledge export" in shipped_words
    assert "per-paper Markdown" not in planned
    assert "answers and project-centred" in planned


def test_release_flow_reviews_metadata_before_tagging_main() -> None:
    release = _read("docs/RELEASE.md")
    lower = release.lower()

    assert "pull request" in lower
    assert "main-reachable" in lower
    assert "git switch main" in release
    assert "git pull --ff-only" in release
    assert "git push origin HEAD vX.Y.Z" not in release
    assert "Never tag a local candidate commit" not in release
    assert "Never point a stable tag at a commit that is not on `main`" in release
    assert release.index("Squash-merge") < release.index('git tag -a "$RELEASE_TAG"')


def test_release_docs_match_the_exact_sha_publish_and_promotion_contract() -> None:
    release = _read("docs/RELEASE.md")
    workflow = _read(".github/workflows/ghcr-publish.yml")
    lower = release.lower()
    release_words = " ".join(release.split())

    assert "source_commit:" in workflow
    assert r"^v[0-9]+\.[0-9]+\.[0-9]+$" in workflow
    assert r"-rc\." not in workflow
    assert '"${GITHUB_REF_NAME}^{commit}"' in workflow
    assert '"$SOURCE_COMMIT" != "$GITHUB_SHA"' in workflow
    assert 'git merge-base --is-ancestor "$SOURCE_COMMIT" origin/main' in workflow
    assert "mode=build-only" in workflow
    assert "mode=verify" in workflow
    assert "mode=promote" in workflow
    assert "actions: read" in workflow
    assert "if: needs.preflight.outputs.mode != 'promote'" in workflow
    assert "if: needs.preflight.outputs.mode == 'verify'" in workflow
    assert "if: needs.preflight.outputs.mode == 'promote'" in workflow
    assert "environment: release" in workflow
    assert "docker buildx imagetools create" in workflow
    assert "source_digest" in workflow
    assert "target_digest" in workflow
    assert 'if [ "$source_digest" != "$target_digest" ]' in workflow
    assert '"${image}@${source_digest}"' in workflow
    assert "verification_run_id:" in workflow
    assert "scripts/release_provenance.py" in workflow
    assert "tag-run-id" in workflow
    assert "validate-run" in workflow
    assert "artifact-digest" in workflow
    assert "actions/runs/${verification_run_id}" in workflow
    build_job = workflow.split("\n  build:", 1)[1].split("\n  verify:", 1)[0]
    verify_job = workflow.split("\n  verify:", 1)[1].split("\n  promote:", 1)[0]
    promote_job = workflow.split("\n  promote:", 1)[1]
    assert (
        "environment: ${{ needs.preflight.outputs.mode == 'verify' && 'release' || '' }}"
        in build_job
    )
    assert "environment: release" in verify_job
    assert "environment: release" in promote_job
    assert "docker/build-push-action@" not in promote_job
    assert "needs.preflight.outputs.source_commit" in promote_job
    assert "needs.preflight.outputs.release_version" in promote_job
    assert "needs.preflight.outputs.verification_run_id" in promote_job
    assert "actions/checkout@" in promote_job
    assert "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c" in (promote_job)
    assert "name: verification-digest-${{ matrix.slug }}" in promote_job
    assert "github-token: ${{ github.token }}" in promote_job
    assert "repository: ${{ github.repository }}" in promote_job
    assert "run-id: ${{ needs.preflight.outputs.verification_run_id }}" in promote_job
    assert "git for-each-ref" not in promote_job
    assert 'imagetools inspect "$source_ref"' not in promote_job
    assert "latest" not in "\n".join(
        line for line in workflow.splitlines() if "imagetools create" in line or "-t " in line
    )

    assert "RELEASE_VERSION=X.Y.Z" in release
    assert 'RELEASE_TAG="v${RELEASE_VERSION}"' in release
    assert "release candidate" not in lower
    assert 'git tag -a "$RELEASE_TAG" "$MERGED_SHA"' in release
    assert 'gh release create "$RELEASE_TAG" --verify-tag' in release
    assert "squash" in lower
    assert 'RELEASE_BRANCH="$(git branch --show-current)"' in release
    assert "git switch -c release/" not in release
    assert release.count('--ref "$RELEASE_BRANCH"') == 2
    assert "Verification-Run-ID: %s" in release
    assert "gh run download" not in release
    assert "digest-*.txt" not in release
    assert 'source_commit="$MERGED_SHA"' in release
    assert 'cold_install_version="$MERGED_SHA"' in release
    assert 'update_from="$UPDATE_FROM"' in release
    assert 'update_to="$MERGED_SHA"' in release
    assert 'update_mode="$UPDATE_MODE"' in release
    assert "downloads its named digest receipt from that exact run" in release_words


def test_release_support_matrix_matches_lifecycle_compatibility_contracts() -> None:
    release = _read("docs/RELEASE.md")
    lifecycle = _read("scripts/lifecycle-smoke.sh")
    workflow = _read(".github/workflows/lifecycle-smoke.yml")
    update_leg = lifecycle.split("run_leg_update() {", 1)[1].split("\n# Leg: uninstall", 1)[0]

    documented = {
        source: (strategy, journal)
        for source, strategy, journal in re.findall(
            r"\| `(v[0-9]+\.[0-9]+\.[0-9]+)` \| "
            r"`([a-z-]+)` \| `([a-z-]+)` \|",
            release,
        )
    }
    expected = {
        "v1.1.3": ("bootstrap", "current-merge-pending"),
        "v1.2.0": ("bootstrap", "current-merge-pending"),
        "v1.2.1": ("bootstrap", "current-merge-pending"),
        # The direct path is what an already-current installation takes, and it
        # exercises the update transaction rather than the bootstrap.
        "v1.2.2": ("direct", "current-merge-pending"),
    }

    assert documented == expected

    # The runbook and the matrix are prose; this input is what a dispatched run
    # actually uses, so a divergence here silently unverifies every source.
    update_mode_input = workflow.split("update_mode:", 1)[1].split("\n\n", 1)[0]
    assert "default: bootstrap" in update_mode_input

    assert 'if [ "$UPDATE_MODE" = bootstrap ]; then' in update_leg
    assert "direct|bootstrap)" in lifecycle
    assert update_leg.count('"${update_command[@]}"') == 2
    assert 'git -C "$clone" show "${to_commit}:scripts/update-bootstrap.sh"' in update_leg
    assert '"phase":"staging"' not in update_leg
    assert '"phase":"merge_pending"' in update_leg
    assert '"schema_version":1' in update_leg
    assert "':(top,exclude)secrets/manifest-hmac-required'" in update_leg
    assert "left unexpected repository paths dirty" in update_leg


def test_local_cross_user_gate_forces_the_root_pytest_config() -> None:
    makefile = _read("Makefile")
    integration_test = _read(
        "services/paper_ingestion/tests/integration/test_cross_user_isolation.py"
    )

    command_fragment = 'uv run pytest -c pyproject.toml -m "integration and live_pg"'
    assert command_fragment in makefile
    assert command_fragment in integration_test


def test_fast_shell_contracts_have_one_make_and_ci_entrypoint() -> None:
    makefile = _read("Makefile")
    workflow = _read(".github/workflows/ci.yml")
    target = makefile.split("\ntest-shell-contracts:", 1)[1].split("\n\n", 1)[0]

    expected_suites = {
        "scripts/tests/test_backup_coverage.sh",
        "scripts/tests/test_restore_coverage.sh",
        "scripts/tests/test_prune_coverage.sh",
        "scripts/tests/test_setup_lib_helpers.sh",
        "scripts/tests/test_update_coverage.sh",
        "scripts/tests/test_update_bootstrap.sh",
        "scripts/tests/test_jarvis_research_cli.sh",
        "scripts/tests/test_uninstall.sh",
    }
    for suite in expected_suites:
        assert target.count(suite) == 1, f"{suite} must have one fast shell entrypoint"

    assert "test_restore_roundtrip.sh" not in target
    assert "test_restore_swap_recovery.sh" not in target
    assert "$(MAKE) test-shell-contracts" in makefile
    assert "make test-shell-contracts" in workflow


def test_local_restore_swap_matrix_uses_the_release_postgres_image() -> None:
    """The isolated destructive matrix must not drift to a mutable DB image."""
    runner = _read("scripts/tests/test_restore_swap_recovery.sh")

    assert POSTGRES_CI_IMAGE in runner
    assert 'CNAME="jarvis-restore-swap-test-${BASHPID}-${RANDOM}"' in runner
    assert "purge_restored_auth_state revert_swap" in runner


def test_live_postgres_jobs_reuse_one_non_skipping_result_validator() -> None:
    workflow = _read(".github/workflows/ci.yml")
    live_litellm_contract = _read(
        "services/paper_ingestion/tests/contract/test_ctx_coupling_contract.py"
    )

    assert workflow.count(f"image: {POSTGRES_CI_IMAGE}") == 2
    assert workflow.count("scripts/check-pytest-junit.py") == 3
    assert "--junitxml=cross-user-isolation.junit.xml" in workflow
    assert "--junitxml=baseline-invariants.junit.xml" in workflow
    assert "--junitxml=contract-tests.junit.xml" in workflow
    assert '-m "contract and not integration and not live_qdrant"' in workflow
    assert (
        "@pytest.mark.integration\nasync def test_num_ctx_write_delivers" in live_litellm_contract
    )
    assert "--collect-only" not in workflow


def test_nightly_qdrant_gate_is_isolated_hosted_and_non_skipping() -> None:
    """The existing nightly workflow owns one real Postgres/Qdrant execution path."""
    workflow = _read(".github/workflows/nightly-llm-smoke.yml")
    runner = _read("scripts/tests/test_corpus_visibility_qdrant.sh")
    root_config = _read("pyproject.toml")
    service_config = _read("services/paper_ingestion/pytest.ini")

    job = workflow.split("  corpus-visibility-qdrant:", 1)[1]
    assert "runs-on: ubuntu-latest" in job
    assert "self-hosted" not in job
    assert f"image: {POSTGRES_CI_IMAGE}" in job
    assert "--tmpfs /var/lib/postgresql/data" in job
    assert 'JARVIS_RUN_LIVE_PG: "1"' in job
    assert "job.services.postgres.ports['5432']" in job
    assert "bash scripts/tests/test_corpus_visibility_qdrant.sh" in job

    assert 'FIXTURE_NAME="jarvis-qdrant-${fixture_suffix}"' in runner
    assert QDRANT_CI_IMAGE in runner
    assert "--publish 127.0.0.1::6333" in runner
    assert "scripts/check-pytest-junit.py" in runner
    assert '--junitxml="$JUNIT_REPORT"' in runner
    assert "contract and live_qdrant" in runner
    assert 'docker rm -f "$FIXTURE_NAME"' in runner
    assert "JARVIS_RUN_LIVE_PG=1 is required; this release gate never skips" in runner

    assert "not live_qdrant" in root_config
    assert "not live_qdrant" in service_config


def test_pytest_result_validator_rejects_zero_passes_and_skips(tmp_path: Path) -> None:
    validator = ROOT / "scripts/check-pytest-junit.py"
    reports = {
        "passing.xml": ('tests="2" failures="0" errors="0" skipped="0"', 0),
        "empty.xml": ('tests="0" failures="0" errors="0" skipped="0"', 1),
        "skipped.xml": ('tests="2" failures="0" errors="0" skipped="1"', 1),
        "failed.xml": ('tests="2" failures="1" errors="0" skipped="0"', 1),
    }

    for filename, (attributes, expected_returncode) in reports.items():
        report = tmp_path / filename
        report.write_text(f"<testsuites><testsuite {attributes}/></testsuites>", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(validator), str(report), "--label", filename],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == expected_returncode, result.stdout + result.stderr


def test_workflow_runner_labels_never_use_self_hosted() -> None:
    workflow_dir = ROOT / ".github/workflows"
    workflows = sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")))
    workflow_text = "\n".join(workflow.read_text(encoding="utf-8") for workflow in workflows)

    assert workflows
    assert "runs-on:" in workflow_text
    assert "self-hosted" not in workflow_text.lower()


def test_remote_workflow_actions_are_commit_pinned() -> None:
    """Every third-party action must use an immutable 40-character commit."""
    workflow_dir = ROOT / ".github/workflows"
    violations: list[str] = []
    for workflow in sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml"))):
        for line_number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
            match = re.match(r"\s*(?:-\s*)?uses:\s*([^\s#]+)", line)
            if match is None:
                continue
            action = match.group(1)
            if action.startswith(("./", "docker://")):
                continue
            _owner, separator, revision = action.rpartition("@")
            if separator != "@" or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
                violations.append(f"{workflow.name}:{line_number}: {action}")

    assert not violations, "workflow actions must be commit-pinned: " + ", ".join(violations)


def test_frontend_parser_fixes_reuse_the_existing_security_job() -> None:
    """Patched parser floors stay coupled to the hosted high-severity audit."""
    package = json.loads(_read("frontend/package.json"))
    overrides = package["overrides"]
    security_workflow = _read(".github/workflows/security.yml")

    assert overrides["brace-expansion@^1"] == "1.1.17"
    assert overrides["brace-expansion@^2"] == "2.1.2"
    assert overrides["brace-expansion@^5"] == "5.0.8"
    assert overrides["js-yaml"] == "^4.3.0"
    assert "runs-on: ubuntu-latest" in security_workflow
    assert "npm ls --prefix frontend js-yaml brace-expansion eslint --all" in security_workflow
    assert "python3 scripts/check_npm_audit.py" in security_workflow


def test_release_guide_routes_every_gate_to_an_existing_execution_path() -> None:
    """The maintainer checklist must name each release-critical CI class."""
    release_guide = _read("docs/RELEASE.md")

    assert "`make check`" in release_guide
    assert "Security / npm-audit" in release_guide
    assert 'gh workflow run nightly-llm-smoke.yml --ref "$RELEASE_BRANCH"' in release_guide
    assert 'gh workflow run lifecycle-smoke.yml --ref "$RELEASE_BRANCH" -f leg=all' in release_guide
    assert "git switch -c release/" not in release_guide
    assert "no skips, failures, or errors" in release_guide
    assert "public-repository workflow" in release_guide
    assert "self-hosted runner" in release_guide


def test_lifecycle_candidate_inputs_are_paired_isolated_and_project_scoped() -> None:
    workflow = _read(".github/workflows/lifecycle-smoke.yml")
    lifecycle = _read("scripts/lifecycle-smoke.sh")

    assert "update_from:" in workflow
    assert "update_to:" in workflow
    assert "update_mode:" in workflow
    assert 'args+=(--update-from "$UPDATE_FROM" --update-to "$UPDATE_TO")' in workflow
    assert 'args+=(--update-mode "$UPDATE_MODE")' in workflow
    assert "--update-from" in lifecycle
    assert "--update-to" in lifecycle
    assert r"^v[0-9]+\.[0-9]+\.[0-9]+$" in lifecycle
    assert r"^[0-9a-f]{40}$" in lifecycle
    assert 'clone="${scratch}/${project}"' in lifecycle
    assert lifecycle.count('clone="${scratch}/${project}"') == 2
    assert '--compose-project-name "$project"' in lifecycle
    assert "assert_project_resources_owned" in lifecycle
    assert "assert_project_absent" in lifecycle
    assert "remove_project_resources" in lifecycle
    assert "project_resource_ids" in lifecycle

    cleanup_helper = lifecycle.split("cleanup_project() {", 1)[1].split("\n}\n\n_tls_cleanup()", 1)[
        0
    ]
    assert 'compose -p "$project" down -v --remove-orphans' in cleanup_helper
    assert 'remove_project_resources "$project"' in cleanup_helper
    assert 'assert_project_absent "$project"' in cleanup_helper
    successful_cleanup = cleanup_helper.split('if [ "$clean" -eq 1 ]; then', 1)[1]
    assert successful_cleanup.index('unregister_project "$project"') < successful_cleanup.index(
        "return 0"
    )
    assert cleanup_helper.rstrip().endswith("return 1")

    driver = lifecycle.split('for leg in "${LEGS[@]}"; do', 1)[1]
    assert driver.index('PROJECT=""') < driver.index('"run_leg_${leg}"')
    assert driver.index('"run_leg_${leg}"') < driver.index('cleanup_project "$PROJECT"')
    assert driver.index('cleanup_project "$PROJECT"') < driver.index('if [ "$leg_rc" -eq 0 ]')
    assert "Lifecycle isolation could not be restored" in driver
    assert "break" in driver

    tls_leg = lifecycle.split("_run_leg_tls_body() {", 1)[1].split("\n# Leg: update", 1)[0]
    uninstall_leg = lifecycle.split("run_leg_uninstall() {", 1)[1].split("\n# Leg: restore", 1)[0]
    assert '--compose-project-name "$project" --build-local' in tls_leg
    assert '--compose-project-name "$project" \\\n      --build-local' in uninstall_leg

    project_helper = lifecycle.split("new_project() {", 1)[1].split("\n}\n\n# new_scratch", 1)[0]
    assert project_helper.index("assert_project_absent") < project_helper.index(
        "CREATED_PROJECTS+="
    )
    absence_helper = lifecycle.split("assert_project_absent() {", 1)[1].split(
        "\n}\n\nassert_project_resources_owned", 1
    )[0]
    assert "project_resource_ids" in absence_helper

    assert 'pending_candidates=("$state"/pending-update*.json)' in lifecycle
    assert '"phase":"staging"' not in lifecycle
    assert '"phase":"merge_pending"' in lifecycle
    assert '"schema_version":1' in lifecycle
    assert "stable_tags | tail -n 2" in lifecycle
    update_leg = lifecycle.split("run_leg_update() {", 1)[1].split("\n# Leg: uninstall", 1)[0]
    pull_shim = lifecycle.split("install_pull_failure_shim() {", 1)[1].split(
        "\n}\n\nrun_leg_update()", 1
    )[0]
    direct_pull_guard = r'if [ "\${1:-}" = "pull" ]; then'
    compose_pull_guard = r'if [ "\${1:-}" = "compose" ]; then'
    assert direct_pull_guard in pull_shim
    assert compose_pull_guard in pull_shim
    assert pull_shim.index(direct_pull_guard) < pull_shim.index(compose_pull_guard)
    assert (
        'grep -q "lifecycle-smoke: image pull blocked by fault injection" '
        '"$injected_log"' in update_leg
    )


def test_lifecycle_candidate_inputs_fail_before_docker_admission() -> None:
    lifecycle = ROOT / "scripts" / "lifecycle-smoke.sh"
    invalid_inputs = (
        (("--update-from", "v1.1.3"), "must be supplied together"),
        (("--update-to", "a" * 40), "must be supplied together"),
        (
            ("--update-from", "v1.1.3-rc.1", "--update-to", "a" * 40),
            "must be a stable vX.Y.Z tag",
        ),
        (
            ("--update-from", "v1.1.3", "--update-to", "A" * 40),
            "must be a lowercase 40-hex commit SHA",
        ),
        (("--update-mode", "automatic"), "must be direct or bootstrap"),
    )

    for arguments, expected_error in invalid_inputs:
        result = subprocess.run(
            ["bash", str(lifecycle), *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert expected_error in result.stderr
        assert "Unknown argument" not in result.stderr
        assert "docker not found" not in result.stderr


def test_cold_install_accepts_release_or_commit_image_identities() -> None:
    workflow = _read(".github/workflows/first-run-smoke.yml")
    cold_job = workflow.split("  cold-install:", 1)[1]

    assert r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$" in cold_job
    assert r"^[0-9a-f]{40}$" in cold_job
    assert "X.Y.Z[-PRERELEASE]" in cold_job
    assert "commit-addressed verification images" in cold_job
    assert cold_job.index("Validate the immutable image identity") < cold_job.index(
        "Free disk space"
    )


def test_destructive_restore_reuses_the_hosted_lifecycle_job() -> None:
    workflow = _read(".github/workflows/lifecycle-smoke.yml")
    lifecycle = _read("scripts/lifecycle-smoke.sh")

    assert "options: [all, tls, update, uninstall, restore]" in workflow
    assert "runs-on: ubuntu-latest" in workflow
    assert "runs-on: self-hosted" not in workflow
    assert "bash scripts/lifecycle-smoke.sh" in workflow
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in workflow
    assert "if: inputs.leg == 'all' || inputs.leg == 'tls'" in workflow

    assert "LEGS=(tls update uninstall restore)" in lifecycle
    assert (
        "env -u COMPOSE_PROJECT_NAME bash scripts/tests/test_restore_roundtrip.sh "
        "--release-gate" in lifecycle
    )
    assert "grep -q '^SKIP:'" in lifecycle
    assert "RESTORE ROUND-TRIP: PASS=" in lifecycle
    assert "FAIL=0" in lifecycle


def test_restore_release_mode_owns_a_generated_compose_project() -> None:
    roundtrip = _read("scripts/tests/test_restore_roundtrip.sh")

    assert "--release-gate" in roundtrip
    assert 'PROJ="jarvis-rt-${fixture_suffix}"' in roundtrip
    assert "^[0-9a-f]{16}$" in roundtrip
    assert "${COMPOSE_PROJECT_NAME+x}" in roundtrip
    assert "_assert_project_absent" in roundtrip
    assert "_project_resources_owned" in roundtrip
    assert "com.docker.compose.project" in roundtrip
    assert "PROJECT_OWNERSHIP_CONFIRMED" in roundtrip
    assert "down -v --remove-orphans" in roundtrip
    assert "fixture project: %s" in roundtrip


def test_restore_release_fixture_contains_current_migration_prerequisites() -> None:
    roundtrip = _read("scripts/tests/test_restore_roundtrip.sh")
    seed = roundtrip.split("seed_jarvis_101() {", 1)[1].split(
        "\n}\n\nseed_auth_restore_contract()", 1
    )[0]

    assert "CREATE TABLE papers(" in seed
    assert "source_type text" in seed
    assert "discovery_origin text NOT NULL" in seed
    for table in (
        "paper_contradictions",
        "paper_user_zotero_links",
        "paper_highlights",
        "paper_summaries",
        "paper_extractions",
        "paper_entities",
        "entity_relationships",
        "paper_notes",
        "cards",
    ):
        assert f"CREATE TABLE {table}(" in seed

    contradiction_table = seed.split("CREATE TABLE paper_contradictions(", 1)[1].split("\n);", 1)[0]
    for column in ("paper_a_id", "paper_b_id", "quote_a", "quote_b", "user_id"):
        assert column in contradiction_table

    zotero_link_table = seed.split("CREATE TABLE paper_user_zotero_links(", 1)[1].split("\n);", 1)[
        0
    ]
    assert "updated_at timestamptz" in zotero_link_table


def test_restore_release_gate_proves_direct_litellm_quarantine() -> None:
    roundtrip = _read("scripts/tests/test_restore_roundtrip.sh")
    fixture_compose = roundtrip.split("cat > \"$WORK/compose.yml\" <<'YAML'\n", 1)[1].split(
        "\nYAML\n", 1
    )[0]

    assert "faux-provider:" in fixture_compose
    assert "litellm:" in fixture_compose
    assert "ports:" not in fixture_compose
    assert (
        "scripts/litellm-entrypoint.sh:/usr/local/bin/litellm-entrypoint.sh:ro" in fixture_compose
    )
    assert (
        'test: ["CMD", "sh", "/usr/local/bin/litellm-entrypoint.sh", "--healthcheck"]'
        in fixture_compose
    )
    assert "backup_trigger:/backup-trigger:ro" in fixture_compose
    assert "./provider-state:/provider-state:ro" in fixture_compose
    assert "provider_hit_count" in roundtrip
    assert "link_host_secret postgres_password" in roundtrip
    assert 'ln -sfn "${name}.txt" "$WORK/host-secrets/$name"' in roundtrip

    baseline = roundtrip.index(
        'wait_for 120 "direct LiteLLM route to the isolated provider" litellm_chat_works'
    )
    quarantined = roundtrip.index(
        'wait_for 60 "direct LiteLLM route to stop during restore review" '
        "litellm_quarantine_blocks_direct_routing"
    )
    recreated = roundtrip.index("dc up -d --force-recreate litellm postgres-backup")
    recovery_control = roundtrip.index(
        'wait_for 120 "restore review control after service recreation" \\\n'
        "        quarantine_recovery_control_available"
    )
    acknowledgement = roundtrip.index('acknowledge_restore_review "$OFF_HOST_RESTORE_ID"')
    resumed = roundtrip.index(
        'wait_for 120 "direct LiteLLM route after exact review acknowledgement" litellm_chat_works'
    )

    assert baseline < quarantined < recreated < recovery_control < acknowledgement < resumed


def test_restore_roundtrip_proves_durable_and_ephemeral_auth_state() -> None:
    roundtrip = _read("scripts/tests/test_restore_roundtrip.sh")

    assert "seed_auth_restore_contract" in roundtrip
    assert "durable_auth_state_survived" in roundtrip
    assert "ephemeral_auth_state_purged" in roundtrip
    assert "webauthn_credentials" in roundtrip
    assert "webauthn_challenges" in roundtrip
    assert "magic_link_tokens" in roundtrip
    assert "telegram_pairing_tokens" in roundtrip
    assert "telegram_user_pairings" in roundtrip
    assert "sessions" in roundtrip


def test_restore_roundtrip_requests_carry_the_required_identity() -> None:
    roundtrip = _read("scripts/tests/test_restore_roundtrip.sh")

    assert r"\"restore_id\":\"${restore_id}\"" in roundtrip
    assert "^[0-9a-f]{32}$" in roundtrip
    assert '_write_restore_request local "$1" omit' in roundtrip
    assert '_write_restore_request inbox "$1" "${2:-omit}"' in roundtrip
    assert "scripts/backup-lifecycle.sh:/usr/local/bin/backup-lifecycle.sh:ro" in roundtrip
    assert "acknowledge_restore_review" in roundtrip
    assert "restore_failed_for_outstanding_review" in roundtrip
