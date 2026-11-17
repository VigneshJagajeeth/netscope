@echo off
setlocal EnableExtensions
REM ===========================================================================
REM  NetScope - build and run everything with Podman  (Windows, Command Prompt)
REM
REM    scripts\podman-up.bat              build image + networks + volume + containers
REM    scripts\podman-up.bat --no-build   skip the build (e.g. after podman pull)
REM    scripts\podman-up.bat --app-only   only (re)create the netscope container
REM
REM  Optional: set IMAGE=docker.io/yourname/netscope:1.0 before running
REM ===========================================================================
cd /d "%~dp0.."
if not defined VERSION set "VERSION=1.0"
if not defined IMAGE set "IMAGE=localhost/netscope:%VERSION%"
if not defined NETSCOPE_PORT set "NETSCOPE_PORT=8080"
set "BUILD=1"
set "APP_ONLY=0"
if /i "%~1"=="--no-build" set "BUILD=0"
if /i "%~1"=="--app-only" set "APP_ONLY=1"
if "%APP_ONLY%"=="1" set "BUILD=0"

echo.
echo == 0. Podman engine ==
call :run podman --version
if errorlevel 1 goto :nopodman
podman info >nul 2>&1
if not errorlevel 1 goto :podman_ok
echo Podman machine is not running - starting it...
podman machine start
podman info >nul 2>&1
if not errorlevel 1 goto :podman_ok
echo No Podman machine yet - creating one. First time only, this downloads about 1 GB...
podman machine init
podman machine start
podman info >nul 2>&1
if errorlevel 1 goto :failed
:podman_ok
set "SOCK="
for /f "usebackq delims=" %%S in (`podman info --format "{{.Host.RemoteSocket.Path}}"`) do set "SOCK=%%S"
if not defined SOCK set "SOCK=/run/podman/podman.sock"
set "SOCK=%SOCK:unix://=%"
echo Podman API socket: %SOCK%

if "%APP_ONLY%"=="1" goto :app
if "%BUILD%"=="0" goto :networks

echo.
echo == 1. Build the custom image from the Containerfile ==
call :run podman build --format docker --build-arg VERSION=%VERSION% -t %IMAGE% -t localhost/netscope:latest -f Containerfile .
if errorlevel 1 goto :failed

:networks
call :run podman image ls --filter reference=netscope

echo.
echo == 2. Podman networks ==
podman network exists ns-frontend || call :run podman network create ns-frontend
podman network exists ns-backend  || call :run podman network create --internal ns-backend
podman network exists ns-isolated || call :run podman network create --internal ns-isolated
call :run podman network ls

echo.
echo == 3. Persistent storage ==
podman volume exists netscope-data || call :run podman volume create netscope-data
if not exist reports mkdir reports
call :run podman volume ls

echo.
echo == 4. Demo micro-service containers ==
podman rm -f demo-web demo-api demo-cache demo-db demo-worker demo-vault >nul 2>&1
call :run podman run -d --name demo-web --network ns-frontend -p 8081:80 docker.io/library/nginx:alpine
if errorlevel 1 goto :failed
call :run podman run -d --name demo-api --network ns-frontend --network ns-backend --expose 8000 docker.io/library/python:3.12-slim python -m http.server 8000
call :run podman run -d --name demo-cache --network ns-backend docker.io/library/redis:7-alpine
call :run podman run -d --name demo-db --network ns-backend -e POSTGRES_PASSWORD=netscope-demo docker.io/library/postgres:16-alpine
REM BUG ON PURPOSE: listens on 127.0.0.1 only, so other containers get "connection refused"
call :run podman run -d --name demo-worker --network ns-backend --expose 9000 docker.io/library/python:3.12-slim python -m http.server 9000 --bind 127.0.0.1
call :run podman run -d --name demo-vault --network ns-isolated docker.io/library/nginx:alpine

:app
echo.
echo == 5. NetScope container: port mapping + volume + bind mount + Podman socket ==
podman rm -f netscope >nul 2>&1
if not exist reports mkdir reports
call :run podman run -d --name netscope --network ns-frontend --network ns-backend -p %NETSCOPE_PORT%:8080 -v netscope-data:/data -v "%CD%\reports:/reports" -v %SOCK%:/run/podman/podman.sock --security-opt label=disable --cap-add NET_RAW --cap-add NET_ADMIN --restart unless-stopped %IMAGE%
if errorlevel 1 goto :failed

echo.
echo == 6. Wait until NetScope is healthy ==
set /a TRIES=0
:wait
set /a TRIES+=1
curl -fsS http://localhost:%NETSCOPE_PORT%/api/health >nul 2>&1
if not errorlevel 1 goto :up
if %TRIES% GEQ 40 goto :noanswer
timeout /t 2 /nobreak >nul
goto :wait

:up
echo NetScope is up.
call :run podman ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}\t{{.Networks}}"
echo.
echo Dashboard: http://localhost:%NETSCOPE_PORT%
if "%APP_ONLY%"=="1" exit /b 0
start "" http://localhost:%NETSCOPE_PORT%
pause
exit /b 0

:noanswer
echo NetScope did not answer on port %NETSCOPE_PORT%. Check the log with:  podman logs netscope
goto :failed

:nopodman
echo Podman is not installed. Install Podman Desktop from https://podman-desktop.io and run this again.
pause
exit /b 1

:failed
echo.
echo *** Something failed - read the error above. ***
pause
exit /b 1

REM ---- prints a command, then runs it ----
:run
echo.
echo ^> %*
%*
exit /b %errorlevel%
