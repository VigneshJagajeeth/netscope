# NetScope — Test Plan & Test Scenario

This plan tests the whole application and every capstone requirement: the
application itself, the Containerfile and custom image, Podman container
management, networking, persistent storage, troubleshooting, and the
registry/GitHub lifecycle.

There are three levels of testing:

| Level | What | How to run | Expected |
|---|---|---|---|
| Unit / API tests | Functions and REST endpoints in isolation | `pytest -q` (on a machine with Python) | **23 passed** |
| End-to-end self-test | The whole running deployment, through the REST API, from inside the container | `podman exec netscope netscope selftest` | **26/26 PASS**, results saved in `reports/` |
| Manual scenario | Podman commands on the host, the browser UI, registry and GitHub | Steps below | As listed in each step |

---

## 1. End-to-end test scenario (user story acceptance test)

> **User story:** *As a developer running several containers on separate
> Podman networks, I want to see which containers can reach which and why a
> connection fails, so that I can fix network problems quickly.*

**Pre-condition:** Podman machine running, project folder open in a terminal.

| Step | Action | Expected result |
|---|---|---|
| 1 | `scripts\podman-up.bat` (Linux: `bash scripts/podman-up.sh`) | Image builds; 3 networks, 1 volume and 7 containers are created; it ends with `Dashboard: http://localhost:8080` |
| 2 | Open http://localhost:8080 | Overview shows the Podman version, 7/7 containers, 2 interfaces, and aardvark-dns |
| 3 | **Topology** page | `demo-api` is linked to frontend and backend; `demo-vault` sits alone on isolated |
| 4 | **Diagnostics → DNS Lookup** `demo-db` | ✅ Resolves to an IP (shared network) |
| 5 | **Diagnostics → DNS Lookup** `demo-vault` | ❌ NXDOMAIN (isolated network, so the name can't be resolved) |
| 6 | **Diagnostics → Port Scan** `demo-api` | Port 8000 open |
| 7 | **Diagnostics → HTTP Check** `http://demo-web` | 200 OK |
| 8 | **Connectivity Matrix → Run matrix** | Green for shared networks, grey for isolated, **red for `demo-worker:9000`** plus 4 findings |
| 9 | Investigate: `podman inspect demo-worker --format "{{.Config.Cmd}}"` | Shows `--bind 127.0.0.1`, the root cause |
| 10 | Fix: `podman rm -f demo-worker`, then re-run it with `--bind 0.0.0.0` (README section 4) | Container recreated |
| 11 | **Run matrix** again | Red cells gone, **0 findings** |
| 12 | **Containers → restart** on `demo-cache` | Toast `restart demo-cache: running -> running`; logged in History |
| 13 | **History** | Every action from steps 4–12 is listed |
| 14 | `podman rm -f netscope`, then `scripts\podman-up.bat --app-only` | NetScope recreated |
| 15 | **History** again | Same entries still there, so the **volume persisted the data** |
| 16 | **History → Save report to /reports** | A `netscope-report-*.json` file appears in the host's `reports` folder (**bind mount**) |
| 17 | `podman exec netscope netscope selftest` | **26/26 tests passed** |

---

## 2. Automated end-to-end self-test (26 test cases)

Run: `podman exec netscope netscope selftest`
Output: a PASS/FAIL table, also saved as `reports/selftest-<time>.txt` and `.json`.

| ID | Area | Test case | Pass criteria |
|---|---|---|---|
| T01 | Application | Web server health check | `/api/health` returns `ok` |
| T02 | Application | Dashboard, static files, API docs | `/`, `app.js`, `style.css` and `/docs` return 200 |
| T03 | Podman | Connected to the Podman API socket | Engine = Podman, version shown |
| T04 | Podman | Self-detection | NetScope finds its own container (`netscope`) |
| T05 | Podman | Discovery | All 7 containers found, all `running` |
| T06 | Networking | Network design | frontend bridge; backend and isolated `internal: true` |
| T07 | Networking | Membership | demo-api on frontend+backend; demo-vault only on isolated |
| T08 | Networking | Port mapping | Host port → container 8080 published |
| T09 | Storage | Volume + bind mount | `/data` is a volume, `/reports` is a bind mount, DB file exists |
| T10 | Networking | Interfaces + DNS | 2+ interfaces (one per network), aardvark-dns detected |
| T11 | Diagnostics | DNS positive | `demo-db` resolves to its real container IP |
| T12 | Diagnostics | DNS negative | `demo-vault` does **not** resolve (isolation) |
| T13 | Diagnostics | Ping | `demo-web` answers |
| T14 | Diagnostics | TCP latency | 5/5 connections to `demo-cache:6379` |
| T15 | Diagnostics | Port scan | On `demo-api` only 8000 is open (8001 and 22 closed) |
| T16 | Diagnostics | HTTP check | `http://demo-web` returns 200 |
| T17 | Diagnostics | Traceroute | Path to `demo-api` found |
| T18 | Security | Input validation | `x; rm -rf /` and a 5000-port scan rejected with HTTP 400 |
| T19 | Networking | Connectivity matrix | Results match the design; the only findings are the planted `demo-worker` bug |
| T20 | Management | Lifecycle | stop → start → pause → unpause → restart all succeed, service back up |
| T21 | Management | Self-protection | NetScope refuses to stop its own container |
| T22 | Troubleshooting | Logs | Log lines returned |
| T23 | Troubleshooting | Inspect + top | Container inspected, processes listed |
| T24 | Troubleshooting | Stats | Network counters for all 7 containers |
| T25 | Storage | History | Every tool run is stored in SQLite on the volume |
| T26 | Storage | Report export | Report file written into the bind-mounted folder |

---

## 3. Manual test cases (Podman on the host)

Fill in *Actual* and *Status* with your own run and paste the screenshot into the report.

| ID | Area | Test case | Command(s) | Expected result | Actual | Status |
|---|---|---|---|---|---|---|
| M01 | Containerfile | Build custom image | `podman build --format docker -t localhost/netscope:1.0 -f Containerfile .` | Build steps run; ends with the image ID | | |
| M02 | Image | Image exists, with metadata | `podman images` · `podman image inspect localhost/netscope:1.0 --format "{{.Labels}}"` · `podman image history localhost/netscope:1.0` | Image listed; version/title labels; one layer per instruction | | |
| M03 | Networking | Networks created | `podman network ls` · `podman network inspect ns-backend` | 3 `ns-*` networks; backend shows `"internal": true` | | |
| M04 | Storage | Volume created | `podman volume ls` · `podman volume inspect netscope-data` | `netscope-data` listed with its mountpoint | | |
| M05 | Management | Containers running | `podman ps` | 7 containers `Up` | | |
| M06 | Networking | Port mapping | `podman port netscope` · open http://localhost:8080 | `8080/tcp -> 0.0.0.0:8080`; dashboard loads | | |
| M07 | Troubleshooting | Health check | `podman healthcheck run netscope` · `podman ps` | Exit code 0; status `(healthy)` | | |
| M08 | Troubleshooting | Logs | `podman logs --tail 10 netscope` | Uvicorn startup and request lines | | |
| M09 | Management | Inspect | `podman inspect netscope --format "{{.HostConfig.RestartPolicy.Name}}"` | `unless-stopped` | | |
| M10 | Troubleshooting | Exec into container | `podman exec netscope netscope interfaces` | 2 interfaces, routes, DNS 10.89.x.1 | | |
| M11 | Management | Lifecycle | `podman stop demo-api` → `podman ps -a` → `podman start demo-api` → `podman restart demo-api` | Status `Exited`, then `Up` | | |
| M12 | Networking | Internal network has no internet | `podman run --rm --network container:demo-db localhost/netscope:1.0 netscope http https://example.com` | Fails (DNS or connection error): backend is internal | | |
| M13 | Networking | Frontend has internet | `podman exec netscope netscope http https://example.com` | 200 OK | | |
| M14 | Networking | Isolation by DNS | `podman exec netscope netscope dns demo-vault` | NXDOMAIN | | |
| M15 | Troubleshooting | Find + fix the planted bug | `podman exec netscope netscope matrix` → fix demo-worker (README section 4) → run the matrix again | 4 findings → 0 findings | | |
| M16 | Storage | Volume persistence | `curl http://localhost:8080/api/history?limit=3` → `podman rm -f netscope` → `scripts\podman-up.bat --app-only` → `curl` again | Same history entries before and after | | |
| M17 | Storage | Bind mount | `podman exec netscope netscope report --save` → `dir reports` | New report file visible on the host | | |
| M18 | Registry | Push | `podman login docker.io` · `podman tag …` · `podman push docker.io/<user>/netscope:1.0` | `Writing manifest to image destination`; tag visible on the registry page | | |
| M19 | Registry | Pull | `podman rmi docker.io/<user>/netscope:1.0` · `podman pull docker.io/<user>/netscope:1.0` | Image downloaded again | | |
| M20 | Registry | Versioning | Build with `--build-arg VERSION=1.1 -t localhost/netscope:1.1` · `podman images` | Both 1.0 and 1.1 listed; labels show the version | | |
| M21 | Application | Run the pulled image | `set IMAGE=docker.io/<user>/netscope:1.0` · `scripts\podman-up.bat --no-build` | Dashboard works from the registry image | | |
| M22 | GitHub | Source published | Open https://github.com/VigneshJagajeeth/netscope | Code, Containerfile and README visible | | |
| M23 | Application | Every UI page | Click through Overview, Containers, Topology, Diagnostics (all 6 tools), Matrix, History | No errors; each tool returns a result | | |
| M24 | Testing | Automated self-test | `podman exec netscope netscope selftest` | 26/26 PASS | | |

---

## 4. Negative and edge cases covered

| Case | Expected behaviour |
|---|---|
| Container on another network | DNS fails (`dns_fail` / NXDOMAIN): shown as "blocked by design", not as an error |
| Service bound to 127.0.0.1 | Connection refused from peers: flagged "should work but fails" |
| Container stopped | Shown as `exited`; the matrix skips it; a **start** button appears |
| Malicious input (`; rm -rf /`) | HTTP 400, nothing executed (no shell is ever used) |
| Scan over 1024 ports | HTTP 400 with a clear message |
| NetScope asked to stop itself | Refused with a clear message |
| Podman socket not mounted | Network tools still work; container views show a banner explaining how to mount the socket |
| `/reports` not mounted | Save report returns a clear error naming the missing bind mount |
