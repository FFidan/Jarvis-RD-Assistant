"""Guard the requirements exporter's dedup rules.

`scripts/export-service-requirements.sh` collapses a dependency group and every
group it includes into one requirements file, keyed by distribution name. Two
specs for the same distribution have to be reconciled rather than silently
resolved, because the file it writes is the human-auditable half of a pair whose
other half is hash-pinned: a dropped extra or a quietly-chosen version would not
show up until an image failed to build.

These cases drive the real script inside a fixture repository, so ROOT_DIR
resolves to the fixture rather than to this checkout and nothing here can write
into the repository. The exporter validates while writing the direct
requirements, which happens before it shells out to `uv export`, so a fixture
needs no lock file to exercise the rules.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
EXPORTER = REPO_ROOT / "scripts" / "export-service-requirements.sh"

_PYPROJECT = """\
[dependency-groups]
paper-ingestion = [{deps}]
paper-ingestion-optional = []
learning-engine = []
telegram-bot = []
"""


def _fixture_repo(tmp_path: Path, deps: str) -> Path:
    """Lay out a minimal repo whose only content is the groups under test."""
    (tmp_path / "scripts").mkdir()
    shutil.copy2(EXPORTER, tmp_path / "scripts" / EXPORTER.name)
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT.format(deps=deps), encoding="utf-8")
    return tmp_path


def _run(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repo / "scripts" / EXPORTER.name)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


def test_same_package_with_different_extras_is_refused(tmp_path: Path) -> None:
    """`x[a]` and `x[b]` have no stronger-vs-weaker answer, so the export must stop.

    Emitting either one drops the other group's extra, and emitting the union
    would no longer match what the lock resolved.
    """
    repo = _fixture_repo(tmp_path, '"uvicorn[standard]>=0.30.0", "uvicorn[watchfiles]>=0.30.0"')
    result = _run(repo)

    assert result.returncode != 0, result.stdout
    assert "different extras" in result.stderr
    assert "uvicorn" in result.stderr
    # The exporter creates the output directory before it validates, so the
    # directory existing proves nothing; the file is what must not appear.
    assert not (repo / "services" / "paper_ingestion" / "requirements.txt").exists(), (
        "a refused export must not leave a requirements file behind"
    )


def test_same_package_with_different_versions_is_refused(tmp_path: Path) -> None:
    """A version disagreement between two groups is a pyproject conflict, not a choice."""
    repo = _fixture_repo(tmp_path, '"httpx>=0.27.0", "httpx>=0.28.0"')
    result = _run(repo)

    assert result.returncode != 0, result.stdout
    assert "conflicting requirements" in result.stderr


@pytest.mark.parametrize(
    ("deps", "expected"),
    [
        ('"uvicorn>=0.30.0", "uvicorn[standard]>=0.30.0"', "uvicorn[standard]>=0.30.0"),
        ('"uvicorn[standard]>=0.30.0", "uvicorn>=0.30.0"', "uvicorn[standard]>=0.30.0"),
    ],
    ids=["bare-then-extras", "extras-then-bare"],
)
def test_the_extras_bearing_spec_wins_regardless_of_order(
    tmp_path: Path, deps: str, expected: str
) -> None:
    """A bare spec and an extras-bearing one are the same requirement, stated twice.

    Order must not decide the outcome, and exactly one line may be emitted --
    a string-keyed dedup would emit both.
    """
    repo = _fixture_repo(tmp_path, deps)
    # `uv export` runs after the direct requirements are written and will fail in
    # a fixture with no lock file; the written file is what this asserts on.
    _run(repo)

    written = (repo / "services" / "paper_ingestion" / "requirements.txt").read_text()
    lines = [line for line in written.splitlines() if not line.startswith("#")]
    assert lines == [expected]
