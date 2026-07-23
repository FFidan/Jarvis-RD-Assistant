"""Text invariants for the install / reconfigure shell scripts.

The repo-root ``test_docker_compose_invariants.py`` covers the compose YAML;
nothing covered the shell-script TEXT invariants that keep a re-run from
destroying operator data. Two guards live here:

  1. ``docker compose down -v`` / ``down --volumes`` (deletes named volumes ->
     Postgres/Qdrant/backups) may appear ONLY in ephemeral-project scripts that
     isolate their compose project, and every such use must carry that isolation
     (``-p`` / ``--project-name`` / ``COMPOSE_PROJECT_NAME``) so it can never hit
     the operator's real deployment.
  2. ``docker {system,image,volume,network,builder} prune`` must never be an
     executed command in these scripts (printed user guidance is fine).

Both are pure text checks: no docker daemon, no network.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# Every shell script on the install / lifecycle surface.
SCRIPTS = sorted(
    p
    for p in (
        [REPO_ROOT / "setup.sh", REPO_ROOT / "update.sh"]
        + list((REPO_ROOT / "scripts").glob("*.sh"))
        + list((REPO_ROOT / "scripts" / "tests").glob("*.sh"))
    )
    if p.is_file()
)

# Files permitted to run `down -v`: each stands up a throwaway compose project,
# except uninstall.sh, whose data tier deliberately targets the operator's real
# project behind typed-confirmation gates (scripts/tests/test_uninstall.sh
# enforces the gates, containment refusals, and dry-run parity).
_DOWN_VOL_ALLOWED_FILES = {
    "scripts/uninstall.sh",
    "scripts/ci-smoke.sh",
    "scripts/first-run-smoke.sh",
    "scripts/lifecycle-smoke.sh",
}
_ISOLATION_EXEMPT_FILES = {"scripts/uninstall.sh"}


def _isolation_required(rel: str) -> bool:
    # Stub-harness suites under scripts/tests/ never reach a real docker
    # daemon; their `down --volumes` mentions are assertion text.
    return rel not in _ISOLATION_EXEMPT_FILES and not rel.startswith("scripts/tests/")


# `down` followed (anywhere later on the same logical command) by -v/--volumes.
_DOWN_VOL = re.compile(r"\bdown\b.*?(?:\s-v(?:\s|$)|\s--volumes\b)")
# Project isolation on a compose invocation.
_ISOLATED = re.compile(r"(?:\s-p[\s=]|--project-name|COMPOSE_PROJECT_NAME)")
# An EXECUTED docker prune (line begins with the command, after optional
# `sudo`/`VAR=x` prefixes, or follows a ;/&&/|| separator). Excludes prune
# words inside quoted hint strings or heredoc help text, which never sit at a
# command boundary.
_PRUNE_CMD = re.compile(
    r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:\w+=\S+\s+)*"
    r"docker\s+(?:system|image|volume|network|builder)\s+prune\b"
)


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _down_vol_allowed(rel: str) -> bool:
    return rel in _DOWN_VOL_ALLOWED_FILES or rel.startswith("scripts/tests/")


def _logical_commands(path: Path):
    """Yield (lineno, text) per logical shell command, joining backslash-newline
    continuations so a multi-line `docker compose ... down -v` reads as one unit."""
    buf: list[str] = []
    start = 0
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if not buf:
            start = lineno
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf.append(stripped[:-1])
            continue
        buf.append(line)
        yield start, " ".join(buf)
        buf = []
    if buf:
        yield start, " ".join(buf)


def test_down_volumes_only_in_isolated_ephemeral_scripts():
    """`down -v` must not touch the real project: banned outside the ephemeral
    scripts, and every allowed use must isolate its compose project."""
    violations = []
    unisolated = []
    for path in SCRIPTS:
        rel = _rel(path)
        for lineno, text in _logical_commands(path):
            if text.lstrip().startswith("#"):
                continue  # prose mention, not an executed command
            if not _DOWN_VOL.search(text):
                continue
            if not _down_vol_allowed(rel):
                violations.append(f"{rel}:{lineno}")
            elif _isolation_required(rel) and not _ISOLATED.search(text):
                unisolated.append(f"{rel}:{lineno}")
    assert not violations, (
        "`down -v` / `down --volumes` runs against the operator's real compose "
        f"project in: {violations}. Named-volume deletion (Postgres/Qdrant/"
        "backups) is only ever allowed in an isolated throwaway project."
    )
    assert not unisolated, (
        "`down -v` in an allowed script is missing project isolation "
        f"(-p / --project-name / COMPOSE_PROJECT_NAME): {unisolated}"
    )


def test_setup_never_advertises_https_on_a_raw_lan_ip():
    """The dashboard nginx serves plain HTTP and LAN mode is HTTP-only, so setup.sh
    must never advertise or probe ``https://`` against the raw LAN IP. An https URL
    on that plaintext endpoint yields SSL_ERROR_RX_RECORD_TOO_LONG, and the setup
    token must never ride raw-IP HTTP — the tokenized link stays on loopback.
    """
    text = (REPO_ROOT / "setup.sh").read_text()
    assert "https://${LAN_IP}" not in text, (
        "setup.sh emits https:// against the raw LAN IP, but that endpoint serves "
        "plain HTTP only — advertise/probe it over http://"
    )


def test_setup_migrates_every_retired_dashboard_tls_key():
    """A reconfigure must remove both inputs from the retired dashboard TLS path."""
    text = (REPO_ROOT / "setup.sh").read_text()
    match = re.search(r'^RETIRED_ENV_KEYS="([^"]*)"', text, re.MULTILINE)
    assert match, "setup.sh must declare RETIRED_ENV_KEYS"
    retired = set(match.group(1).split())
    assert {"JARVIS_CERT_SAN", "JARVIS_SKIP_SELFSIGNED_GEN"}.issubset(retired), (
        "setup.sh must remove both obsolete dashboard certificate inputs from "
        f"carried-forward .env files; got {sorted(retired)}"
    )


def test_no_executed_docker_prune():
    """The install / reconfigure scripts must never auto-run a docker prune;
    printed guidance telling the operator to prune manually is fine."""
    violations = []
    for path in SCRIPTS:
        rel = _rel(path)
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _PRUNE_CMD.search(line):
                violations.append(f"{rel}:{lineno}: {line.strip()}")
    assert not violations, (
        "Executed `docker <system|image|volume|network|builder> prune` found "
        f"(installer must not prune the host's Docker resources): {violations}"
    )
