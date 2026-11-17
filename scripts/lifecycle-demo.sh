#!/usr/bin/env bash
# =============================================================================
#  Container management & troubleshooting walk-through for the live demo.
#  Run after ./scripts/podman-up.sh.  Press Enter to go to the next command.
# =============================================================================
cd "$(dirname "$0")/.."
PORT="${NETSCOPE_PORT:-8080}"
run()   { printf '\n\033[1;36m$ %s\033[0m\n' "$*"; "$@"; }
pause() { printf '\n\033[2m[Enter] %s\033[0m' "${1:-next}"; read -r _; }

echo "== Inspect & monitor =="
run podman ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}";            pause
run podman inspect netscope --format "IP(s): {{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}} | Restart: {{.HostConfig.RestartPolicy.Name}}"; pause
run podman port netscope;                                                        pause
run podman logs --tail 15 netscope;                                              pause
run podman top netscope;                                                         pause
run podman stats --no-stream;                                                    pause
run podman healthcheck run netscope && echo healthy;                             pause

echo "== Lifecycle =="
run podman stop demo-api;  run podman ps -a --filter name=demo-api;              pause
run podman start demo-api; run podman ps --filter name=demo-api;                 pause
run podman restart demo-cache;                                                   pause
run podman exec netscope netscope containers;                                    pause

echo "== Troubleshooting from inside the network =="
run podman exec netscope netscope dns demo-db
run podman exec netscope netscope dns demo-vault                                  # isolated -> NXDOMAIN
pause
run podman exec netscope netscope scan demo-worker -p 9000                        # refused (bound to 127.0.0.1)
run podman exec demo-worker python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9000').status)"
pause "run the connectivity matrix"
run podman exec netscope netscope matrix;                                        pause

echo "== Persistent storage =="
run podman volume inspect netscope-data;                                         pause
run curl -s "http://localhost:$PORT/api/history?limit=3"; echo
run podman rm -f netscope
./scripts/podman-up.sh --app-only
run curl -s "http://localhost:$PORT/api/history?limit=3"; echo
echo "-> the history is still there: it lives in the netscope-data volume, not in the container."
run podman exec netscope netscope report --save
run ls -l reports
echo "-> /reports is a bind mount: the file appears in the project's reports/ folder on the host."
