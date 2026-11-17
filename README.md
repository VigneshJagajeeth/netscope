# NetScope — Container Network Diagnostics Toolkit

**21CSE216P – Linux and Container Technologies · Capstone Project**
Built, containerised and deployed with **Podman**.

NetScope is a containerised web application for inspecting and troubleshooting
container networks. It runs as a Podman container next to other containers,
discovers them through the Podman API, maps which networks they share, manages
them (logs, inspect, start/stop/restart), and runs live diagnostics: ping, TCP
latency, DNS, port scans, HTTP checks, traceroute, and a
**container-to-container connectivity matrix** that compares the real network
behaviour with the network design.

It comes in three parts:

* **Web dashboard** at `http://localhost:8080`
* **REST API** with interactive docs at `http://localhost:8080/docs`
* **CLI** (`podman exec netscope netscope …`)

---

## User story

> *As a developer running several containers on separate Podman networks,
> I want to see which containers can reach which, and why a connection fails,
> so that I can fix network problems without manually exec-ing into every
> container with ping, dig and curl.*

**Problem statement.** Container networking is invisible. When service A
cannot reach service B, the cause could be DNS (the containers share no
network), segmentation by design (an internal network), a service that is down,
or an app bound to the wrong address. Minimal images often have no network tools
to investigate with.

**Objectives**
1. Containerise a diagnostics application with a custom **Containerfile** and **Podman image**.
2. Discover containers, networks, IPs, ports and mounts through the Podman API.
3. Provide network tests (ping, latency, DNS, port scan, HTTP, traceroute) from inside the container network.
4. Automatically verify container-to-container reachability against the network design.
5. Manage containers (logs, inspect, top, start/stop/restart/pause) from one dashboard.
6. Keep results in **persistent storage** (Podman volume) and export reports to the host (**bind mount**).
7. Publish the image to a public registry and the source to GitHub.

---

## Capstone requirements checklist

| Guideline requirement | Where it is in this project |
|---|---|
| Small working application | NetScope web app + REST API + CLI (`netscope/`) |
| Custom **Containerfile** (FROM, RUN, CMD, EXPOSE…) | [`Containerfile`](Containerfile): FROM, ARG, LABEL, ENV, RUN, WORKDIR, COPY, VOLUME, EXPOSE, HEALTHCHECK, CMD |
| Custom **Podman image** | `podman build -t localhost/netscope:1.0 -f Containerfile .` |
| At least one running container | `netscope` + 6 demo containers (`podman ps`) |
| **Podman network** | `ns-frontend`, `ns-backend` (internal), `ns-isolated` (internal) |
| **Persistent storage** – volume | `netscope-data` volume → `/data` (SQLite history survives `podman rm`) |
| **Persistent storage** – bind mount | `./reports` → `/reports` (reports saved from the UI appear on the host) |
| **Port mapping** | `-p 8080:8080` (dashboard), `-p 8081:80` (demo-web) |
| Container management | `podman ps / inspect / logs / top / stats / stop / start / restart / rm`, plus the **Manage** buttons in the dashboard |
| Troubleshooting | `podman logs`, `podman exec`, `podman healthcheck run`, NetScope diagnostics + matrix, and a deliberately broken container (`demo-worker`) to find and fix |
| Testing | `podman exec netscope netscope selftest` (26 end-to-end tests) + `pytest` (23 unit tests) + [docs/TEST-PLAN.md](docs/TEST-PLAN.md) |
| Lifecycle: push/pull, versioning | `podman tag` → `podman push` → `podman pull`; tags `1.0`, `1.1`, `latest` |
| README with commands | This file. Every command is listed below. |
| GitHub upload | See section 7 |
| Live demonstration | See section 8 and `scripts/lifecycle-demo.bat` |

---

## Architecture

```
 Windows host (browser → http://localhost:8080)
 └── Podman machine (WSL2 Linux VM)   ── podman.socket (REST API) ─────────────┐
      │                                                                        │
      │  ┌─────────────── netscope container (localhost/netscope:1.0) ───────┐ │
      │  │ FastAPI web server + dashboard + CLI                              │◀┘ -v <podman.sock>:/run/podman/podman.sock
      │  │ diagnostics.py │ engine.py (Podman API) │ store.py (SQLite)      │
      │  │   /data  ◀── volume netscope-data        /reports ◀── bind ./reports
      │  └───────┬──────────────────────────────┬───────────────────────────┘
      │     ns-frontend (bridge)          ns-backend (bridge, --internal)       ns-isolated (--internal)
      │     ├ demo-web (nginx) :8081      ├ demo-api (python)                  └ demo-vault (nginx)
      │     └ demo-api                    ├ demo-cache (redis)
      │                                   ├ demo-db (postgres)
      │                                   └ demo-worker (python, bug: binds 127.0.0.1)
      │
      │  Connectivity matrix: for each container X, NetScope starts a short-lived
      │  probe container with  --network container:X  (joins X's network namespace),
      │  tests every other container's ports, then removes the probe.
```

**DNS.** On user-defined Podman networks, **aardvark-dns** runs on the network
gateway (for example `10.89.0.1`) and resolves container names, but only for
containers on the same network. That's why `demo-web` cannot resolve `demo-db`.

**Matrix colours.** 🟩 reachable and expected (shared network) · ⬛ blocked by
design (no shared network → `dns_fail`) · 🟥 should work but fails (service down
or bound to the wrong address) · 🟨 reachable without a shared network (through a
published port).

---

## Project structure

```
netscope/
├── Containerfile              # custom image definition (Podman/Buildah)
├── .containerignore           # files excluded from the build context
├── compose.yaml               # optional: podman compose up
├── requirements.txt / pyproject.toml
├── netscope/
│   ├── app.py                 # FastAPI REST API + serves the dashboard
│   ├── cli.py                 # `netscope` command-line tool
│   ├── diagnostics.py         # ping, latency, DNS, scan, HTTP, traceroute, interfaces
│   ├── engine.py              # Podman API: discovery, management, stats, matrix
│   ├── store.py               # SQLite history (in the /data volume)
│   ├── selftest.py            # end-to-end self-test (netscope selftest)
│   └── static/                # dashboard (HTML/CSS/JS, no build step)
├── docs/TEST-PLAN.md          # test scenario + test cases
├── scripts/
│   ├── podman-up.bat / .sh    # build + network + volume + run (prints every command)
│   ├── podman-down.bat / .sh  # remove containers + networks (keeps the volume)
│   └── lifecycle-demo.bat/.sh # management, troubleshooting & persistence walk-through
├── reports/                   # bind-mounted into the container (created on first run)
└── tests/test_netscope.py     # pytest suite
```

---

## 1. Install Podman (Windows)

WSL must already be installed (`wsl --install`, then restart). Then, in Command Prompt:

```bat
winget install -e --id RedHat.Podman
```

Close and reopen Command Prompt, then create and start the Podman machine
(a small Linux VM in WSL2):

```bat
podman machine init
podman machine start
podman info
```

(Optional GUI: `winget install -e --id RedHat.Podman-Desktop`.)

> If Docker Desktop is also running NetScope, stop it first (`docker compose down`)
> so port 8080 is free.

---

## 2. Quick start: one command

From the project folder (the one containing `Containerfile`):

```bat
scripts\podman-up.bat
```

Linux/macOS: `./scripts/podman-up.sh`

The script prints each `podman` command before running it, then opens
**http://localhost:8080**. The commands it runs are listed in section 3.

---

## 3. All commands, step by step

### 3.1 Build the custom image

```bash
podman build --format docker --build-arg VERSION=1.0 -t localhost/netscope:1.0 -t localhost/netscope:latest -f Containerfile .
podman images
podman image inspect localhost/netscope:1.0
podman image history localhost/netscope:1.0
```

`--format docker` keeps the `HEALTHCHECK` instruction; the default OCI format drops it.

### 3.2 Create the Podman networks

```bash
podman network create ns-frontend
podman network create --internal ns-backend      # no internet access
podman network create --internal ns-isolated
podman network ls
podman network inspect ns-backend
```

### 3.3 Create persistent storage

```bash
podman volume create netscope-data
podman volume ls
podman volume inspect netscope-data
mkdir reports                                    # host folder for the bind mount
```

### 3.4 Run the demo containers

```bash
podman run -d --name demo-web    --network ns-frontend -p 8081:80 docker.io/library/nginx:alpine
podman run -d --name demo-api    --network ns-frontend --network ns-backend --expose 8000 docker.io/library/python:3.12-slim python -m http.server 8000
podman run -d --name demo-cache  --network ns-backend docker.io/library/redis:7-alpine
podman run -d --name demo-db     --network ns-backend -e POSTGRES_PASSWORD=netscope-demo docker.io/library/postgres:16-alpine
podman run -d --name demo-worker --network ns-backend --expose 9000 docker.io/library/python:3.12-slim python -m http.server 9000 --bind 127.0.0.1
podman run -d --name demo-vault  --network ns-isolated docker.io/library/nginx:alpine
```

### 3.5 Run NetScope

First find the Podman API socket path (inside the Podman machine):

```bash
podman info --format "{{.Host.RemoteSocket.Path}}"
# rootless: /run/user/1000/podman/podman.sock    rootful: /run/podman/podman.sock
```

Then run NetScope (Windows cmd; replace `<SOCKET>` with the path above):

```bat
podman run -d --name netscope ^
  --network ns-frontend --network ns-backend ^
  -p 8080:8080 ^
  -v netscope-data:/data ^
  -v "%CD%\reports:/reports" ^
  -v <SOCKET>:/run/podman/podman.sock ^
  --security-opt label=disable ^
  --cap-add NET_RAW --cap-add NET_ADMIN ^
  --restart unless-stopped ^
  localhost/netscope:1.0
```

Linux/macOS: use `\` instead of `^` and `"$PWD/reports:/reports"`.

| Option | Why |
|---|---|
| `--network` ×2 | NetScope joins both networks so it can test containers on each |
| `-p 8080:8080` | port mapping: host 8080 → container 8080 |
| `-v netscope-data:/data` | named volume: the SQLite history survives container removal |
| `-v ./reports:/reports` | bind mount: saved reports appear in the host folder |
| `-v <socket>:/run/podman/podman.sock` | lets NetScope query and manage containers through the Podman API |
| `--security-opt label=disable` | allows socket access on SELinux hosts |
| `--cap-add NET_RAW` | raw sockets for `ping` / `traceroute` / `tcpdump` |
| `--restart unless-stopped` | restart policy |

Open **http://localhost:8080**.

---

## 4. Container management & troubleshooting

```bash
podman ps                                   # running containers
podman ps -a                                # all containers, including stopped
podman inspect netscope                     # full configuration (JSON)
podman inspect netscope --format "{{.NetworkSettings.Networks}}"
podman port netscope                        # port mappings
podman logs netscope                        # application logs
podman logs -f --tail 20 netscope           # follow logs
podman top netscope                         # processes in the container
podman stats --no-stream                    # CPU / memory / network I/O
podman healthcheck run netscope             # run the HEALTHCHECK now
podman exec -it netscope bash               # shell inside the container
podman stop demo-api                        # lifecycle
podman start demo-api
podman restart demo-api
podman pause demo-api ; podman unpause demo-api
podman rm -f demo-api                       # remove
podman events --since 10m --stream=false    # engine event log
```

The same actions are in the dashboard (**Containers → logs / inspect / restart /
pause / stop / start**), and in the CLI:

```bash
podman exec netscope netscope engine                 # which engine, version, socket
podman exec netscope netscope containers
podman exec netscope netscope networks
podman exec netscope netscope interfaces              # interfaces, routes, DNS (aardvark)
podman exec netscope netscope ping demo-web -c 3
podman exec netscope netscope latency demo-cache -p 6379 -c 10
podman exec netscope netscope dns demo-db             # resolves (shared network)
podman exec netscope netscope dns demo-vault          # NXDOMAIN (isolated)
podman exec netscope netscope scan demo-api -p 1-1024
podman exec netscope netscope http http://demo-web
podman exec netscope netscope trace demo-api
podman exec netscope netscope matrix                  # finds the demo-worker bug
podman exec netscope netscope logs demo-worker
podman exec netscope netscope manage demo-api restart
podman exec netscope netscope report --save           # writes to the bind mount
podman exec netscope netscope history
podman exec netscope netscope selftest              # 26 end-to-end tests
```

**Debugging inside any container's network namespace** (for images with no tools):

```bash
podman run --rm -it --network container:demo-web localhost/netscope:1.0 netscope scan localhost
podman run --rm -it --network container:demo-web localhost/netscope:1.0 bash   # ip, dig, curl, tcpdump
```

**Troubleshooting case study: demo-worker.** The matrix shows every peer
getting *refused* on `demo-worker:9000`, even though they share `ns-backend`.

```bash
podman exec netscope netscope scan demo-worker -p 9000        # closed from outside
podman exec demo-worker python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9000').status)"   # 200 inside
podman inspect demo-worker --format "{{.Config.Cmd}}"          # ... --bind 127.0.0.1
```

The cause is that the server listens on 127.0.0.1 (the container's own
loopback), not 0.0.0.0. To fix it:

```bash
podman rm -f demo-worker
podman run -d --name demo-worker --network ns-backend --expose 9000 docker.io/library/python:3.12-slim python -m http.server 9000 --bind 0.0.0.0
podman exec netscope netscope matrix                          # finding gone
```

---

## 5. Persistent storage demonstration

```bash
curl http://localhost:8080/api/history?limit=3         # some diagnostic runs exist
podman rm -f netscope                                  # delete the container
scripts\podman-up.bat --app-only                       # recreate it (same volume)
curl http://localhost:8080/api/history?limit=3         # the same runs are still there
podman volume inspect netscope-data                    # where the data lives

podman exec netscope netscope report --save            # or History → "Save report to /reports"
dir reports                                            # file appears on the host (bind mount)
```

---

## 6. Registry: push, pull and versioning

**Docker Hub** (repositories are public by default):

```bash
podman login docker.io
podman tag localhost/netscope:1.0 docker.io/<user>/netscope:1.0
podman tag localhost/netscope:1.0 docker.io/<user>/netscope:latest
podman push docker.io/<user>/netscope:1.0
podman push docker.io/<user>/netscope:latest
```

Registry link: `https://hub.docker.com/r/<user>/netscope`

**Quay.io** (used in DO188). After the first push, make the repository public in
its settings:

```bash
podman login quay.io
podman tag localhost/netscope:1.0 quay.io/<user>/netscope:1.0
podman push quay.io/<user>/netscope:1.0
```

**Versioning: release 1.1**

```bash
podman build --format docker --build-arg VERSION=1.1 -t localhost/netscope:1.1 -f Containerfile .
podman tag localhost/netscope:1.1 docker.io/<user>/netscope:1.1
podman tag localhost/netscope:1.1 docker.io/<user>/netscope:latest
podman push docker.io/<user>/netscope:1.1
podman push docker.io/<user>/netscope:latest
podman image inspect localhost/netscope:1.1 --format "{{.Labels}}"
```

**Pull and run from the registry** (proves the published image works):

```bat
podman rmi docker.io/<user>/netscope:1.0
podman pull docker.io/<user>/netscope:1.0
set IMAGE=docker.io/<user>/netscope:1.0
scripts\podman-up.bat --no-build
```

---

## 7. Upload to GitHub

1. Create an **empty** repository named `netscope` on https://github.com (no README).
2. In the project folder:

```bash
git init
git add .
git commit -m "NetScope - container network diagnostics toolkit (Podman)"
git branch -M main
git remote add origin https://github.com/<user>/netscope.git
git push -u origin main
```

(No Git installed? On the repository page, use **Add file → Upload files** and
drag in the project folder contents.)

---

## 8. Live demo plan (about 10 minutes)

1. **Build:** `podman build …` and `podman images` (custom image from the Containerfile).
2. **Network + storage:** `podman network ls`, `podman network inspect ns-backend` (internal), `podman volume ls`.
3. **Run:** `podman ps`, open http://localhost:8080 (port mapping).
4. **Overview:** two interfaces (one per network), routes, and aardvark-dns on the gateway IP.
5. **Topology:** `demo-api` bridges frontend and backend; `demo-vault` is isolated.
6. **Diagnostics:** DNS `demo-db` ✓ → DNS `demo-vault` ✗ (isolation); port scan `demo-api`; HTTP `http://demo-web`.
7. **Connectivity matrix:** red cells on `demo-worker:9000` → troubleshoot (section 4) → fix → re-run → clean.
8. **Management:** Containers → logs / inspect / restart; then `podman logs`, `podman inspect`, `podman stop/start` in the terminal.
9. **Persistence:** `podman rm -f netscope` → recreate → the history is still there; Save report → file in `reports/`.
10. **Registry + GitHub:** show the Docker Hub/Quay page and the GitHub repository.

`scripts\lifecycle-demo.bat` walks through steps 8–9 command by command (press a key between steps).

---

## REST API

Interactive docs: **http://localhost:8080/docs**

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/health` | Health check (used by HEALTHCHECK) |
| GET | `/api/overview` | Interfaces, routes, DNS, engine status, recent runs |
| GET | `/api/engine/status` | Engine (Podman/Docker), version, socket, rootless |
| GET | `/api/containers` | Containers with networks, IPs, ports, mounts |
| GET | `/api/containers/{name}/logs?tail=200` | Container logs |
| GET | `/api/containers/{name}/inspect` | Full inspect JSON |
| GET | `/api/containers/{name}/top` | Processes |
| POST | `/api/containers/{name}/{start\|stop\|restart\|pause\|unpause}` | Lifecycle action |
| GET | `/api/networks` | Networks with members |
| GET | `/api/topology` | Graph data |
| GET | `/api/stats` | Per-container network I/O |
| POST | `/api/tools/{ping\|latency\|dns\|scan\|http\|traceroute}` | Diagnostics |
| POST | `/api/matrix` | Connectivity matrix |
| GET/DELETE | `/api/history` | Stored runs |
| GET | `/api/report` | Download the JSON report |
| POST | `/api/report/save` | Save the report into `/reports` (bind mount) |

---

## Testing

The full test scenario and test cases (for the report) are in **[docs/TEST-PLAN.md](docs/TEST-PLAN.md)**.

**End-to-end self-test of the running deployment (26 test cases):**

```bash
podman exec netscope netscope selftest
```

It checks the web app, the Podman API connection, container discovery, networks
(including the internal flags), port mapping, volume + bind mount, every
diagnostic tool, the connectivity matrix, lifecycle actions (stop, start,
pause, restart), logs, inspect, top, stats, history persistence and report
export. It prints a PASS/FAIL table and saves it to `reports/selftest-*.txt`.

Unit and API tests (no container engine needed):

```bash
pip install -r requirements.txt pytest
pytest -q
```

Smoke test against the running container:

```bash
curl http://localhost:8080/api/health
curl -X POST http://localhost:8080/api/tools/scan -H "Content-Type: application/json" -d "{\"host\":\"demo-api\",\"ports\":\"8000\"}"
podman healthcheck run netscope
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `podman: command not found` | Install Podman (section 1) and reopen the terminal |
| `Cannot connect to Podman` | `podman machine start` |
| Dashboard says *Engine: not connected* | The socket path is wrong. Check `podman info --format "{{.Host.RemoteSocket.Path}}"` and use exactly that path in `-v` |
| `address already in use` on 8080 | Something else uses the port (for example Docker): `docker compose down`, or `set NETSCOPE_PORT=9090` before running the script |
| `short-name resolution enforced` | Always use full image names (`docker.io/library/nginx:alpine`), as this project does |
| Ping says *ICMP not permitted* | Add `--cap-add NET_RAW` |
| Matrix shows *probe image not found* | Run NetScope from a locally built or pulled image |
| `denied: requested access` on push | `podman login docker.io` first; the tag must start with *your* username |
| Quay image not public | Quay.io → repository → Settings → Make public |

## Security note

Mounting the Podman socket gives NetScope control over your containers.
With **rootless Podman** (the default on Windows/macOS) that is limited to your
own user's containers and is not root on the host, which is one advantage of
Podman over Docker. Still, keep the dashboard on localhost and never expose it
to the internet. NetScope validates every hostname and port it receives and
never passes user input through a shell.
