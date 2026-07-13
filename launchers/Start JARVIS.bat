@echo off
REM Double-click launcher for Windows. Opens a terminal window and runs the
REM canonical bootstrap (setup.sh) via WSL, falling back to Git Bash if WSL
REM is not available. Always pauses at the end so setup.sh's exit code and
REM any message (especially exit code 3, "log out and back in") stay visible
REM instead of the window silently closing.
REM
REM Uses goto/labels rather than multi-line if (...) blocks on purpose: batch
REM expands %variables% once when a parenthesized block is parsed, not as
REM each line inside it runs, so reading %errorlevel% right after a command
REM inside the same block would see the block's OLD errorlevel instead of
REM that command's actual exit code.

cd /d "%~dp0.."

where wsl >nul 2>nul
if not %errorlevel%==0 goto :try_bash
wsl bash ./setup.sh
set STATUS=%errorlevel%
goto :report

:try_bash
where bash >nul 2>nul
if not %errorlevel%==0 goto :no_shell
bash ./setup.sh
set STATUS=%errorlevel%
goto :report

:no_shell
echo Could not find WSL or Git Bash to run setup.sh.
echo Docker Desktop for Windows runs on WSL2, so installing it is the supported path:
echo   https://learn.microsoft.com/windows/wsl/install
echo Then re-run this launcher (it will use WSL automatically).
set STATUS=1
goto :end

:report
echo.
if "%STATUS%"=="0" goto :success
if "%STATUS%"=="3" goto :docker_just_installed
echo setup.sh exited with status %STATUS%. See the output above for details.
goto :end

:success
echo Setup finished.
goto :end

:docker_just_installed
echo Docker was just installed. Log out and back in (or run 'newgrp docker' inside WSL), then double-click this launcher again.
goto :end

:end
echo.
pause
exit /b %STATUS%
