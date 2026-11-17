#!/usr/bin/env bash
# =============================================================================
#  NetScope - build and run everything with Podman (Linux / macOS / WSL)
#
#  ./scripts/podman-up.sh               build image + networks + volume + containers
#  ./scripts/podman-up.sh --no-build    skip the build (e.g. after `podman pull` from a registry)
#  ./scripts/podman-up.sh --app-only    only (re)create the netscope container
#
#  Env: VERSION=1.0  IMAGE=localhost/netscope:1.0  NETSCOPE_PORT=8080
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${VERSION:-1.0}"
IMAGE="${IMAGE:-localhost/netscope:${VERSION}}"
PORT="${NETSCOPE_PORT:-8080}"
BUILD=1; APP_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --no-build) BUILD=0 ;;
    --app-only) APP_ONLY=1; BUILD=0 ;;
  esac
done

run()  { printf '\n\033[1;36m$ %s\033[0m\n' "$*"; "$@"; }
step() { printf '\n\033[1;33m== %s ==\033[0m\n' "$*"; }

# ---------------------------------------------------------------- 0. Podman
step "0. Podman engine"
run podman --version
if ! podman info >/dev/null 2>&1; then
  echo "Podman is not responding - trying 'podman machine start' (macOS/Windows)"
  podman machine start || true
fi
SOCK="$(podman info --format '{{.Host.RemoteSocket.Path}}' 2>/dev/null || true)"
SOCK="${SOCK#unix://}"
SOCK="${SOCK:-/run/podman/podman.sock}"
if [ "$(uname)" = "Linux" ] && [ ! -S "$SOCK" ]; then
  echo "Podman API socket $SOCK is not active - enabling podman.socket"
  mkdir -p "$(dirname "$SOCK")"
  if [ "$(id -u)" = "0" ]; then
    systemctl enable --now podman.socket 2>/dev/null || (nohup podman system service --time=0 "unix://$SOCK" >/dev/null 2>&1 &)
  else
    systemctl --user enable --now podman.socket 2>/dev/null || (nohup podman system service --time=0 "unix://$SOCK" >/dev/null 2>&1 &)
  fi
  sleep 2
fi
echo "Podman API socket: $SOCK"

if [ "$APP_ONLY" = "0" ]; then
  # -------------------------------------------------------------- 1. Image
  if [ "$BUILD" = "1" ]; then
    step "1. Build the custom image from the Containerfile"
    run podman build --format docker --build-arg VERSION="$VERSION" \
        -t "$IMAGE" -t localhost/netscope:latest -f Containerfile .
  fi
  run podman image ls --filter reference=netscope

  # -------------------------------------------------------------- 2. Networks
  step "2. Podman networks"
  podman network exists ns-frontend || run podman network create ns-frontend
  podman network exists ns-backend  || run podman network create --internal ns-backend
  podman network exists ns-isolated || run podman network create --internal ns-isolated
  run podman network ls

  # -------------------------------------------------------------- 3. Storage
  step "3. Persistent storage"
  podman volume exists netscope-data || run podman volume create netscope-data
  mkdir -p reports
  run podman volume ls

  # -------------------------------------------------------------- 4. Demo stack
  step "4. Demo micro-service containers"
  podman rm -f demo-web demo-api demo-cache demo-db demo-worker demo-vault >/dev/null 2>&1 || true
  run podman run -d --name demo-web    --network ns-frontend -p 8081:80 docker.io/library/nginx:alpine
  run podman run -d --name demo-api    --network ns-frontend --network ns-backend --expose 8000 \
        docker.io/library/python:3.12-slim python -m http.server 8000
  run podman run -d --name demo-cache  --network ns-backend docker.io/library/redis:7-alpine
  run podman run -d --name demo-db     --network ns-backend -e POSTGRES_PASSWORD=netscope-demo \
        docker.io/library/postgres:16-alpine
  # BUG ON PURPOSE: listens on 127.0.0.1 only -> other containers get "connection refused"
  run podman run -d --name demo-worker --network ns-backend --expose 9000 \
        docker.io/library/python:3.12-slim python -m http.server 9000 --bind 127.0.0.1
  run podman run -d --name demo-vault  --network ns-isolated docker.io/library/nginx:alpine
fi

# ---------------------------------------------------------------- 5. NetScope
step "5. NetScope container (port mapping + volume + bind mount + Podman socket)"
podman rm -f netscope >/dev/null 2>&1 || true
mkdir -p reports
run podman run -d --name netscope \
    --network ns-frontend --network ns-backend \
    -p "$PORT":8080 \
    -v netscope-data:/data \
    -v "$PWD/reports":/reports \
    -v "$SOCK":/run/podman/podman.sock \
    --security-opt label=disable \
    --cap-add NET_RAW --cap-add NET_ADMIN \
    --restart unless-stopped \
    "$IMAGE"

# ---------------------------------------------------------------- 6. Verify
step "6. Wait until NetScope is healthy"
for i in $(seq 1 40); do
  if curl -fsS "http://localhost:$PORT/api/health" >/dev/null 2>&1; then
    echo "NetScope is up."; break
  fi
  sleep 2
  [ "$i" = 40 ] && { echo "NetScope did not answer - check: podman logs netscope"; exit 1; }
done
run podman ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}\t{{.Networks}}"
printf '\n\033[1;32mDashboard: http://localhost:%s\033[0m\n' "$PORT"
