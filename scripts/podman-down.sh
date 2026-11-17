#!/usr/bin/env bash
# Stop and remove NetScope + demo containers and networks.
# The netscope-data volume is KEPT (persistent storage) unless you pass --purge.
cd "$(dirname "$0")/.."
run() { printf '\n\033[1;36m$ %s\033[0m\n' "$*"; "$@"; }

run podman rm -f netscope demo-web demo-api demo-cache demo-db demo-worker demo-vault
run podman network rm ns-frontend ns-backend ns-isolated
if [ "${1:-}" = "--purge" ]; then
  run podman volume rm netscope-data
else
  echo; echo "Volume netscope-data kept (history survives). Use --purge to delete it."
fi
