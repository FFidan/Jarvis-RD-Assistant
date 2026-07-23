@echo off
REM Double-click launcher for Windows. Opens a terminal window and runs the
REM canonical bootstrap (setup.sh) through the supported WSL2 path. Always
REM pauses at the end so setup.sh's exit code and any message (especially exit
REM code 3, "log out and back in") stay visible instead of the window silently
REM closing.
REM
REM Uses goto/labels rather than multi-line if (...) blocks on purpose: batch
REM expands %variables% once when a parenthesized block is parsed, not as
REM each line inside it runs, so reading %errorlevel% right after a command
REM inside the same block would see the block's OLD errorlevel instead of
REM that command's actual exit code.

where wsl >nul 2>nul
if not %errorlevel%==0 goto :no_wsl
wsl --cd "%~dp0.." env JARVIS_WINDOWS_LAUNCHER=1 bash ./setup.sh
set STATUS=%errorlevel%
goto :report

:no_wsl
echo JARVIS on Windows needs WSL2. Git Bash cannot run this setup safely.
echo 1. Open PowerShell as Administrator and run: wsl --install
echo 2. Restart Windows if asked, then open Ubuntu once to finish setup.
echo 3. In Docker Desktop, open Settings ^> Resources ^> WSL Integration.
echo 4. Enable Ubuntu, click Apply ^& restart, then run this launcher again.
echo Help:
echo   https://learn.microsoft.com/windows/wsl/install
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
