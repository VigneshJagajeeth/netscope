@echo off
REM Stop and remove NetScope + demo containers and networks.
REM The netscope-data volume is KEPT (persistent storage) unless you pass --purge.
cd /d "%~dp0.."
echo ^> podman rm -f netscope demo-web demo-api demo-cache demo-db demo-worker demo-vault
podman rm -f netscope demo-web demo-api demo-cache demo-db demo-worker demo-vault
echo ^> podman network rm ns-frontend ns-backend ns-isolated
podman network rm ns-frontend ns-backend ns-isolated
if /i "%~1"=="--purge" goto :purge
echo.
echo Volume netscope-data kept - the history survives. Use --purge to delete it.
exit /b 0
:purge
echo ^> podman volume rm netscope-data
podman volume rm netscope-data
