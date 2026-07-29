#!/usr/bin/env python3
"""Validate the immutable evidence used to promote release images."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, NoReturn

RUN_ID_PREFIX = "Verification-Run-ID:"
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class ProvenanceError(ValueError):
    """Raised when release evidence does not satisfy the publication contract."""


def verification_run_id(tag_message: str) -> int:
    """Return the single verification run ID recorded in an annotated tag."""
    declarations = [line for line in tag_message.splitlines() if line.startswith(RUN_ID_PREFIX)]
    if len(declarations) != 1:
        raise ProvenanceError(
            "the annotated tag must contain exactly one Verification-Run-ID declaration"
        )
    match = re.fullmatch(r"Verification-Run-ID: ([1-9][0-9]*)", declarations[0])
    if match is None:
        raise ProvenanceError(
            "Verification-Run-ID must be a positive decimal GitHub Actions run ID"
        )
    return int(match.group(1))


def validate_verification_run(
    run: dict[str, Any],
    *,
    expected_sha: str,
    workflow_path: str,
) -> None:
    """Require a successful main dispatch of the expected workflow and commit."""
    if COMMIT_PATTERN.fullmatch(expected_sha) is None:
        raise ProvenanceError("expected SHA must be lowercase 40-hex")

    expected_fields = {
        "path": workflow_path,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": expected_sha,
        "status": "completed",
        "conclusion": "success",
    }
    mismatches = [
        f"{field}={run.get(field)!r}"
        for field, expected in expected_fields.items()
        if run.get(field) != expected
    ]
    if mismatches:
        raise ProvenanceError(
            "verification run does not match the release: " + ", ".join(mismatches)
        )


def verification_digest(artifact_dir: Path, *, expected_ref: str) -> str:
    """Return the digest from one exact, regular verification receipt."""
    try:
        entries = list(artifact_dir.iterdir())
    except OSError as exc:
        raise ProvenanceError(f"verification artifact is unreadable: {exc}") from exc

    if len(entries) != 1 or not entries[0].is_file() or entries[0].is_symlink():
        raise ProvenanceError("verification artifact must contain exactly one regular receipt file")

    try:
        receipt = entries[0].read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ProvenanceError(f"verification receipt is unreadable: {exc}") from exc

    match = re.fullmatch(rf"{re.escape(expected_ref)}@(sha256:[0-9a-f]{{64}})\n?", receipt)
    if match is None:
        raise ProvenanceError(
            "verification receipt does not match the expected image reference and digest"
        )
    digest = match.group(1)
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise ProvenanceError("verification receipt contains an invalid digest")
    return digest


def _fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate release verification run and artifact provenance."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    tag = commands.add_parser("tag-run-id")
    tag.add_argument("tag_message", type=Path)

    run = commands.add_parser("validate-run")
    run.add_argument("run_json", type=Path)
    run.add_argument("--expected-sha", required=True)
    run.add_argument("--workflow-path", required=True)

    artifact = commands.add_parser("artifact-digest")
    artifact.add_argument("artifact_dir", type=Path)
    artifact.add_argument("--expected-ref", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the selected provenance check."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "tag-run-id":
            print(verification_run_id(args.tag_message.read_text(encoding="utf-8")))
        elif args.command == "validate-run":
            document = json.loads(args.run_json.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ProvenanceError("verification run response must be a JSON object")
            validate_verification_run(
                document,
                expected_sha=args.expected_sha,
                workflow_path=args.workflow_path,
            )
        else:
            print(
                verification_digest(
                    args.artifact_dir,
                    expected_ref=args.expected_ref,
                )
            )
    except (OSError, UnicodeError, json.JSONDecodeError, ProvenanceError) as exc:
        _fail(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
