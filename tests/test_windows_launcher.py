"""Static contracts for the double-click Windows launcher."""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHER = _REPO_ROOT / "launchers" / "Start JARVIS.bat"


def _launcher_text() -> str:
    return _LAUNCHER.read_text(encoding="ascii")


def test_windows_launcher_never_falls_back_to_native_bash() -> None:
    launcher = _launcher_text()

    assert "falling back to Git Bash" not in launcher
    assert "where bash" not in launcher
    assert "\nbash ./setup.sh" not in launcher
    assert (
        'wsl --cd "%~dp0.." env JARVIS_WINDOWS_LAUNCHER=1 bash ./setup.sh'
        in launcher
    )


def test_windows_launcher_explains_missing_wsl_without_reporting_success() -> None:
    launcher = _launcher_text()
    assert "\n:no_wsl\n" in launcher
    missing_wsl = launcher.split("\n:no_wsl\n", 1)[1].split("\n:report\n", 1)[0]

    assert "wsl --install" in missing_wsl
    assert "Settings ^> Resources ^> WSL Integration" in missing_wsl
    assert "set STATUS=1" in missing_wsl
    assert missing_wsl.index("set STATUS=1") < missing_wsl.index("goto :end")
    assert "goto :report" not in missing_wsl
    assert "Setup finished" not in missing_wsl


def test_windows_launcher_preserves_setup_status_and_pause() -> None:
    launcher = _launcher_text()

    assert "set STATUS=%errorlevel%" in launcher
    assert 'if "%STATUS%"=="3" goto :docker_just_installed' in launcher
    assert "pause" in launcher
    assert "exit /b %STATUS%" in launcher
