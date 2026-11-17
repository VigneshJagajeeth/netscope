@echo off
setlocal EnableExtensions
title NetScope - one-click setup (Podman)
cd /d "%~dp0"
echo ============================================================
echo   NetScope - one-click setup with Podman
echo ============================================================
echo.
echo == Step 1 of 3: free ports 8080/8081 from the old Docker version ==
where docker >nul 2>&1
if errorlevel 1 goto :podman_check
docker rm -f netscope demo-web demo-api demo-cache demo-db demo-worker demo-vault >nul 2>&1
echo Done.

:podman_check
echo.
echo == Step 2 of 3: Podman ==
where podman >nul 2>&1
if not errorlevel 1 goto :have_podman
if exist "%ProgramFiles%\RedHat\Podman\podman.exe" goto :add_path
echo Podman is not installed - installing it now with winget.
echo If Windows asks for permission, click Yes.
winget install -e --id RedHat.Podman --accept-package-agreements --accept-source-agreements
:add_path
set "PATH=%PATH%;%ProgramFiles%\RedHat\Podman"
where podman >nul 2>&1
if errorlevel 1 goto :reopen
:have_podman
podman --version

echo.
echo == Step 3 of 3: build and run NetScope - first run takes a few minutes ==
call "%~dp0scripts\podman-up.bat"
exit /b %errorlevel%

:reopen
echo.
echo Podman was installed, but this window cannot see it yet.
echo Close this window and double-click RUN-ME.bat again.
pause
exit /b 1
