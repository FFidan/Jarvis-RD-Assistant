#!/usr/bin/env python3
"""Run the locally reproducible subset of the hosted security workflow."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_ROOT = Path(
    os.environ.get(
        "JARVIS_SECURITY_CACHE_DIR",
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "jarvis-rd"
        / "security-tools",
    )
).expanduser()


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Immutable download and executable integrity contract for one scanner."""

    name: str
    version: str
    url: str
    artifact_name: str
    artifact_sha256: str
    executable_name: str
    executable_sha256: str
    archive_member: str | None = None


OSV_SCANNER = ToolSpec(
    name="osv-scanner",
    version="2.0.2",
    url=("https://github.com/google/osv-scanner/releases/download/v2.0.2/osv-scanner_linux_amd64"),
    artifact_name="osv-scanner-v2.0.2-linux-amd64",
    artifact_sha256="3abcfd7126c453a00421487e721b296e0cb68085bd431d6cef60872774170fc8",
    executable_name="osv-scanner-v2.0.2-linux-amd64",
    executable_sha256="3abcfd7126c453a00421487e721b296e0cb68085bd431d6cef60872774170fc8",
)

GITLEAKS = ToolSpec(
    name="gitleaks",
    version="8.30.1",
    url=(
        "https://github.com/gitleaks/gitleaks/releases/download/"
        "v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz"
    ),
    artifact_name="gitleaks_8.30.1_linux_x64.tar.gz",
    artifact_sha256="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    executable_name="gitleaks-v8.30.1-linux-x64",
    executable_sha256="88f91962aa2f93ac6ab281d553b9e125f5197bbbce38f9f2437f7299c32e5509",
    archive_member="gitleaks",
)

TOOLS = (OSV_SCANNER, GITLEAKS)
REQUIRED_INPUTS = (
    Path(".gitleaks.toml"),
    Path("frontend/package-lock.json"),
    Path("osv-scanner.toml"),
    Path("scripts/check_npm_audit.py"),
    Path("services/learning_engine/constraints.txt"),
    Path("services/paper_ingestion/constraints.txt"),
    Path("services/telegram_bot/constraints.txt"),
)


class SecurityScanError(RuntimeError):
    """Raised when a scanner or required scan input cannot be trusted."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_digest(path: Path, expected: str, label: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise SecurityScanError(f"missing or unsafe {label}: {path}")
    actual = _sha256(path)
    if actual != expected:
        raise SecurityScanError(f"corrupt {label}: expected SHA-256 {expected}, got {actual}")


def _verify_executable(path: Path, expected: str, label: str) -> None:
    _verify_digest(path, expected, label)
    if not os.access(path, os.X_OK):
        raise SecurityScanError(f"non-executable {label}: {path}")


def _validate_platform() -> None:
    if sys.platform != "linux" or os.uname().machine not in {"x86_64", "amd64"}:
        raise SecurityScanError(
            "the pinned local scanner artifacts support Linux x86_64 only; "
            "use the hosted Security workflow on another platform"
        )


def _validate_inputs() -> None:
    missing = [str(path) for path in REQUIRED_INPUTS if not (REPO_ROOT / path).is_file()]
    if missing:
        raise SecurityScanError("missing security scan inputs: " + ", ".join(missing))


def _validated_cache_root() -> Path:
    cache_root = DEFAULT_CACHE_ROOT.resolve()
    try:
        cache_root.relative_to(REPO_ROOT)
    except ValueError:
        pass
    else:
        raise SecurityScanError("security tool cache must be outside the repository")
    cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return cache_root


def _download_artifact(spec: ToolSpec, cache_root: Path) -> Path:
    destination = cache_root / spec.artifact_name
    if destination.exists():
        _verify_digest(destination, spec.artifact_sha256, f"{spec.name} artifact")
        return destination

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{spec.artifact_name}.", dir=cache_root)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        subprocess.run(
            [
                "curl",
                "--proto",
                "=https",
                "--tlsv1.2",
                "--fail",
                "--show-error",
                "--location",
                "--output",
                str(temporary),
                spec.url,
            ],
            check=True,
        )
        _verify_digest(temporary, spec.artifact_sha256, f"downloaded {spec.name} artifact")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _install_executable(spec: ToolSpec, artifact: Path, cache_root: Path) -> Path:
    executable = cache_root / spec.executable_name
    if executable.exists():
        _verify_digest(executable, spec.executable_sha256, f"{spec.name} executable")
        executable.chmod(0o700)
        return executable

    if spec.archive_member is None:
        if artifact != executable:
            shutil.copyfile(artifact, executable)
    else:
        with tarfile.open(artifact, mode="r:gz") as archive:
            member = archive.getmember(spec.archive_member)
            if not member.isfile():
                raise SecurityScanError(
                    f"{spec.name} archive member is not a regular file: {spec.archive_member}"
                )
            source = archive.extractfile(member)
            if source is None:
                raise SecurityScanError(
                    f"cannot read {spec.name} archive member: {spec.archive_member}"
                )
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{spec.executable_name}.", dir=cache_root
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as target:
                    shutil.copyfileobj(source, target)
                _verify_digest(
                    temporary,
                    spec.executable_sha256,
                    f"extracted {spec.name} executable",
                )
                os.replace(temporary, executable)
            finally:
                temporary.unlink(missing_ok=True)

    _verify_digest(executable, spec.executable_sha256, f"{spec.name} executable")
    executable.chmod(0o700)
    return executable


def _prepare_tools(cache_root: Path) -> dict[str, Path]:
    installed: dict[str, Path] = {}
    for spec in TOOLS:
        artifact = _download_artifact(spec, cache_root)
        installed[spec.name] = _install_executable(spec, artifact, cache_root)
    return installed


def _verify_cached_tools(cache_root: Path) -> dict[str, Path]:
    installed: dict[str, Path] = {}
    for spec in TOOLS:
        artifact = cache_root / spec.artifact_name
        executable = cache_root / spec.executable_name
        _verify_digest(artifact, spec.artifact_sha256, f"{spec.name} artifact")
        _verify_executable(executable, spec.executable_sha256, f"{spec.name} executable")
        installed[spec.name] = executable
    return installed


def _run(command: list[str]) -> None:
    subprocess.run(command, cwd=REPO_ROOT, check=True)


def _run_scans(tools: dict[str, Path]) -> None:
    for constraints in REQUIRED_INPUTS[-3:]:
        _run(
            [
                "uvx",
                "pip-audit",
                "--no-deps",
                "--disable-pip",
                "-r",
                str(constraints),
            ]
        )
    _run([sys.executable, "scripts/check_npm_audit.py"])
    _run([str(tools["osv-scanner"]), "scan", "--recursive", "."])
    _run(
        [
            str(tools["gitleaks"]),
            "detect",
            "--source",
            ".",
            "--config",
            ".gitleaks.toml",
            "--redact",
            "--exit-code",
            "1",
            "--verbose",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--prepare-tools-only",
        action="store_true",
        help="download and verify the pinned scanners without running them",
    )
    mode.add_argument(
        "--verify-cache-only",
        action="store_true",
        help="verify the existing scanner cache without downloading or running it",
    )
    args = parser.parse_args()

    try:
        _validate_platform()
        _validate_inputs()
        cache_root = _validated_cache_root()
        if args.verify_cache_only:
            _verify_cached_tools(cache_root)
            return 0
        tools = _prepare_tools(cache_root)
        if args.prepare_tools_only:
            return 0
        _run_scans(tools)
    except (OSError, SecurityScanError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"security scan failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
