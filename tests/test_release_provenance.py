"""Behavioral tests for stable-image release provenance."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "release_provenance.py"
SOURCE_SHA = "a" * 40
SOURCE_REF = f"ghcr.io/limitcycle-oss/jarvis-dashboard:{SOURCE_SHA}"
WORKFLOW_PATH = ".github/workflows/ghcr-publish.yml"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _successful_run() -> dict[str, Any]:
    return {
        "path": WORKFLOW_PATH,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": SOURCE_SHA,
        "status": "completed",
        "conclusion": "success",
    }


def _write_run(path: Path, run: dict[str, Any]) -> None:
    path.write_text(json.dumps(run), encoding="utf-8")


def test_tag_message_has_one_positive_verification_run_id(tmp_path: Path) -> None:
    message = tmp_path / "tag-message.txt"
    message.write_text("Release v2.3.4\n\nVerification-Run-ID: 123456789\n", encoding="utf-8")

    result = _run("tag-run-id", str(message))

    assert result.returncode == 0
    assert result.stdout == "123456789\n"


@pytest.mark.parametrize(
    "message",
    [
        "Release v2.3.4\n",
        "Verification-Run-ID: 0\n",
        "Verification-Run-ID: abc\n",
        "Verification-Run-ID: 12\nVerification-Run-ID: 13\n",
    ],
)
def test_tag_message_rejects_missing_malformed_or_duplicate_run_ids(
    tmp_path: Path, message: str
) -> None:
    message_path = tmp_path / "tag-message.txt"
    message_path.write_text(message, encoding="utf-8")

    assert _run("tag-run-id", str(message_path)).returncode == 1


def test_verification_run_matches_workflow_main_commit_and_success(
    tmp_path: Path,
) -> None:
    run_path = tmp_path / "run.json"
    _write_run(run_path, _successful_run())

    result = _run(
        "validate-run",
        str(run_path),
        "--expected-sha",
        SOURCE_SHA,
        "--workflow-path",
        WORKFLOW_PATH,
    )

    assert result.returncode == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", ".github/workflows/ci.yml"),
        ("event", "push"),
        ("head_branch", "feature/release"),
        ("head_sha", "b" * 40),
        ("status", "in_progress"),
        ("conclusion", "failure"),
    ],
)
def test_verification_run_rejects_each_provenance_mismatch(
    tmp_path: Path, field: str, value: str
) -> None:
    run = _successful_run()
    run[field] = value
    run_path = tmp_path / "run.json"
    _write_run(run_path, run)

    result = _run(
        "validate-run",
        str(run_path),
        "--expected-sha",
        SOURCE_SHA,
        "--workflow-path",
        WORKFLOW_PATH,
    )

    assert result.returncode == 1
    assert field in result.stderr


def test_receipt_returns_the_exact_recorded_digest(tmp_path: Path) -> None:
    digest = f"sha256:{'c' * 64}"
    (tmp_path / "digest-dashboard.txt").write_text(f"{SOURCE_REF}@{digest}\n", encoding="utf-8")

    result = _run(
        "artifact-digest",
        str(tmp_path),
        "--expected-ref",
        SOURCE_REF,
    )

    assert result.returncode == 0
    assert result.stdout == f"{digest}\n"


@pytest.mark.parametrize(
    "receipt",
    [
        f"ghcr.io/limitcycle-oss/jarvis-dashboard:{'b' * 40}@sha256:{'c' * 64}\n",
        f"{SOURCE_REF}@sha256:{'C' * 64}\n",
        f"{SOURCE_REF}@sha256:{'c' * 63}\n",
        f"{SOURCE_REF}@sha256:{'c' * 64}\nextra\n",
    ],
)
def test_receipt_rejects_wrong_reference_malformed_digest_or_extra_data(
    tmp_path: Path, receipt: str
) -> None:
    (tmp_path / "digest-dashboard.txt").write_text(receipt, encoding="utf-8")

    result = _run(
        "artifact-digest",
        str(tmp_path),
        "--expected-ref",
        SOURCE_REF,
    )

    assert result.returncode == 1


def test_receipt_requires_exactly_one_regular_file(tmp_path: Path) -> None:
    empty_result = _run(
        "artifact-digest",
        str(tmp_path),
        "--expected-ref",
        SOURCE_REF,
    )
    assert empty_result.returncode == 1

    receipt = f"{SOURCE_REF}@sha256:{'c' * 64}\n"
    (tmp_path / "first.txt").write_text(receipt, encoding="utf-8")
    (tmp_path / "second.txt").write_text(receipt, encoding="utf-8")

    multiple_result = _run(
        "artifact-digest",
        str(tmp_path),
        "--expected-ref",
        SOURCE_REF,
    )
    assert multiple_result.returncode == 1


def test_receipt_rejects_a_symbolic_link(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text(f"{SOURCE_REF}@sha256:{'c' * 64}\n", encoding="utf-8")
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "digest.txt").symlink_to(target)

    result = _run(
        "artifact-digest",
        str(artifact),
        "--expected-ref",
        SOURCE_REF,
    )

    assert result.returncode == 1
