@echo off
setlocal EnableExtensions
REM ===========================================================================
REM  Container management + troubleshooting walk-through for the live demo.
REM  Run after scripts\podman-up.bat. Press a key to go to the next command.
REM ===========================================================================
cd /d "%~dp0.."
if not defined NETSCOPE_PORT set "NETSCOPE_PORT=8080"

echo == Inspect and monitor ==
call :run podman ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
pause
call :run podman inspect netscope --format "IPs: {{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}} - Restart policy: {{.HostConfig.RestartPolicy.Name}}"
pause
call :run podman port netscope
call :run podman logs --tail 15 netscope
pause
call :run podman top netscope
call :run podman stats --no-stream
pause
call :run podman healthcheck run netscope
if not errorlevel 1 echo healthy
pause

echo.
echo == Lifecycle ==
call :run podman stop demo-api
call :run podman ps -a --filter name=demo-api
pause
call :run podman start demo-api
call :run podman restart demo-cache
call :run podman ps --filter name=demo
pause
call :run podman exec netscope netscope containers
pause

echo.
echo == Troubleshooting from inside the network ==
call :run podman exec netscope netscope dns demo-db
call :run podman exec netscope netscope dns demo-vault
pause
call :run podman exec netscope netscope scan demo-worker -p 9000
call :run podman exec demo-worker python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9000').status)"
echo -^> works inside demo-worker, refused from outside: the app is bound to 127.0.0.1
pause
call :run podman exec netscope netscope matrix
pause

echo.
echo == Persistent storage ==
call :run podman volume inspect netscope-data
call :run curl -s "http://localhost:%NETSCOPE_PORT%/api/history?limit=3"
echo.
pause
call :run podman rm -f netscope
call "%~dp0podman-up.bat" --app-only
call :run curl -s "http://localhost:%NETSCOPE_PORT%/api/history?limit=3"
echo.
echo -^> The history is still there: it lives in the netscope-data volume, not in the container.
pause
call :run podman exec netscope netscope report --save
call :run dir reports
echo -^> /reports is a bind mount, so the file appears in the project's reports folder on Windows.
pause
exit /b 0

:run
echo.
echo ^> %*
%*
exit /b %errorlevel%
