"""End-to-end self-test of the running NetScope deployment.

Run it inside the container:

    podman exec netscope netscope selftest

It exercises the whole application through its REST API against the real
demo stack: web UI, Podman API connection, container/network discovery,
port mapping, volume + bind mount storage, every diagnostic tool, the
connectivity matrix, container lifecycle management, logs/inspect/top,
history persistence and report export. Results are printed as a PASS/FAIL
table and saved into /reports (the bind mount) as evidence.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

import httpx

EXPECTED = ["netscope", "demo-web", "demo-api", "demo-cache", "demo-db", "demo-worker", "demo-vault"]
GREEN, RED, YELLOW, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"


class Check(Exception):
    """Raised by a test step with the reason it failed."""


def expect(cond, msg: str):
    if not cond:
        raise Check(msg)


class SelfTest:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=120)
        self.results: list[dict] = []
        self.cache: dict = {}

    # ---------------------------------------------------------------- helpers
    def get(self, path, **kw):
        r = self.http.get(path, **kw)
        return r

    def post(self, path, body=None):
        return self.http.post(path, json=body) if body is not None else self.http.post(path)

    def tool(self, name, body):
        r = self.post(f"/api/tools/{name}", body)
        expect(r.status_code == 200, f"HTTP {r.status_code}: {r.text[:120]}")
        return r.json()

    def api_json(self, path, **kw):
        r = self.get(path, **kw)
        if r.status_code == 503:
            raise Check("Podman API not connected (see T03) - check the socket mount")
        expect(r.status_code == 200, f"GET {path}: HTTP {r.status_code} {r.text[:100]}")
        return r.json()

    def containers(self, refresh=False):
        if refresh or "containers" not in self.cache:
            self.cache["containers"] = {c["name"]: c for c in self.api_json("/api/containers")}
        return self.cache["containers"]

    def run(self, tid: str, area: str, name: str, fn):
        start = time.perf_counter()
        try:
            detail = fn() or ""
            status = "PASS"
        except Check as exc:
            status, detail = "FAIL", str(exc)
        except Exception as exc:  # noqa: BLE001
            status, detail = "FAIL", f"{type(exc).__name__}: {exc}"[:200]
        ms = round((time.perf_counter() - start) * 1000)
        self.results.append({"id": tid, "area": area, "test": name, "status": status,
                             "detail": detail, "ms": ms})
        color = GREEN if status == "PASS" else RED
        print(f"  {tid}  {color}{status}{RESET}  {name:<58} {detail}"[:200], flush=True)

    # ---------------------------------------------------------------- tests
    def t_health(self):
        d = self.get("/api/health").json()
        expect(d.get("status") == "ok", f"health = {d}")
        return f"version {d.get('version')}"

    def t_dashboard(self):
        r = self.get("/")
        expect(r.status_code == 200 and "NetScope" in r.text, "dashboard HTML not served")
        for asset in ("/static/app.js", "/static/style.css"):
            expect(self.get(asset).status_code == 200, f"{asset} missing")
        expect(self.get("/docs").status_code == 200, "API docs missing")
        return "index.html, app.js, style.css, /docs served"

    def t_engine(self):
        d = self.get("/api/engine/status").json()
        expect(d.get("available"), f"engine not reachable: {d.get('error', '')[:100]}")
        self.cache["engine"] = d
        return f"{d['engine']} {d['server_version']} via {d.get('socket')}" + (" (rootless)" if d.get("rootless") else "")

    def t_self(self):
        d = self.cache.get("engine") or self.get("/api/engine/status").json()
        expect(d.get("self_container") == "netscope", f"self container = {d.get('self_container')}")
        return "NetScope identified its own container"

    def t_discovery(self):
        cs = self.containers(refresh=True)
        missing = [n for n in EXPECTED if n not in cs]
        expect(not missing, f"missing containers: {missing}")
        down = [n for n in EXPECTED if cs[n]["status"] != "running"]
        expect(not down, f"not running: {down}")
        return f"all {len(EXPECTED)} containers running"

    def _net(self, suffix):
        if "networks" not in self.cache:
            self.cache["networks"] = {n["name"]: n for n in self.api_json("/api/networks")}
        nets = self.cache["networks"]
        for name, n in nets.items():
            if name.endswith(suffix):
                return n
        raise Check(f"network *{suffix} not found")

    def t_networks(self):
        fe, be, iso = self._net("frontend"), self._net("backend"), self._net("isolated")
        expect(not fe["internal"], f"{fe['name']} should NOT be internal")
        expect(be["internal"], f"{be['name']} should be internal")
        expect(iso["internal"], f"{iso['name']} should be internal")
        return f"{fe['name']} (bridge), {be['name']} (internal), {iso['name']} (internal)"

    def t_membership(self):
        cs = self.containers()
        nets = lambda c: {n["name"].split("_")[-1].split("-")[-1] for n in cs[c]["networks"]}  # noqa: E731
        expect(nets("demo-api") >= {"frontend", "backend"}, f"demo-api on {nets('demo-api')}")
        expect(nets("demo-vault") == {"isolated"}, f"demo-vault on {nets('demo-vault')}")
        expect(nets("netscope") >= {"frontend", "backend"}, f"netscope on {nets('netscope')}")
        return "demo-api bridges frontend+backend, demo-vault isolated"

    def t_ports(self):
        pub = self.containers()["netscope"]["published_ports"]
        expect(any(p["container"].startswith("8080") for p in pub), f"8080 not published: {pub}")
        host = [p["host_port"] for p in pub if p["container"].startswith("8080")][0]
        return f"host port {host} -> container 8080"

    def t_storage(self):
        mounts = self.containers()["netscope"]["mounts"]
        vol = [m for m in mounts if m["destination"] == "/data"]
        bind = [m for m in mounts if m["destination"] == "/reports"]
        expect(vol and vol[0]["type"] == "volume", f"/data is not a volume: {vol}")
        expect(bind and bind[0]["type"] == "bind", f"/reports is not a bind mount: {bind}")
        expect(os.path.exists(os.environ.get("NETSCOPE_DB", "/data/netscope.db")), "SQLite DB not found in /data")
        return f"volume {vol[0]['source']} -> /data, bind -> /reports, DB present"

    def t_interfaces(self):
        d = self.get("/api/interfaces").json()
        ifs = [i for i in d["interfaces"] if i["name"] != "lo" and i["ipv4"]]
        expect(len(ifs) >= 2, f"expected 2+ interfaces (one per network), got {len(ifs)}")
        expect(d["dns"]["container_dns"], f"container DNS not detected: {d['dns']['nameservers']}")
        return f"{len(ifs)} interfaces, DNS = {d['dns']['container_dns_label']}"

    def t_dns_ok(self):
        r = self.tool("dns", {"name": "demo-db"})
        expect(r["ok"] and r["answers"], f"demo-db did not resolve: {r.get('error')}")
        ips = {n["ip"] for n in self.containers()["demo-db"]["networks"]}
        expect(r["answers"][0] in ips, f"resolved {r['answers'][0]} but container has {ips}")
        return f"demo-db -> {r['answers'][0]} ({r['query_ms']} ms)"

    def t_dns_isolated(self):
        r = self.tool("dns", {"name": "demo-vault"})
        expect(not r["ok"], f"demo-vault should NOT resolve but got {r.get('answers')}")
        return f"demo-vault not resolvable ({r.get('error')}) - isolation works"

    def t_ping(self):
        r = self.tool("ping", {"host": "demo-web", "count": 3})
        expect(r["ok"], f"ping failed: {r.get('error')}")
        return r["summary"]

    def t_latency(self):
        r = self.tool("latency", {"host": "demo-cache", "port": 6379, "count": 5})
        expect(r["ok"] and r["received"] == 5, r["summary"])
        return f"avg {r['avg_ms']} ms, jitter {r['jitter_ms']} ms"

    def t_scan(self):
        r = self.tool("scan", {"host": "demo-api", "ports": "8000,8001,22"})
        open_ports = [p["port"] for p in r["open"]]
        expect(open_ports == [8000], f"expected only 8000 open, got {open_ports}")
        return f"8000 open, 8001/22 closed ({r['duration_s']} s)"

    def t_http(self):
        r = self.tool("http", {"url": "http://demo-web"})
        expect(r.get("status_code") == 200, f"got {r.get('status_code')} {r.get('error', '')}")
        return f"200 OK in {r['timings']['total_ms']} ms"

    def t_trace(self):
        r = self.tool("traceroute", {"host": "demo-api", "max_hops": 5})
        expect(r["ok"] and r["hops"], f"traceroute failed: {r.get('error')}")
        return f"{len(r['hops'])} hop(s), reached={r['reached']}"

    def t_validation(self):
        r = self.post("/api/tools/ping", {"host": "x; rm -rf /"})
        expect(r.status_code == 400, f"bad input returned {r.status_code}")
        r2 = self.post("/api/tools/scan", {"host": "demo-api", "ports": "1-5000"})
        expect(r2.status_code == 400, f"oversized scan returned {r2.status_code}")
        return "malicious host and oversized scan rejected with 400"

    def t_matrix(self):
        r = self.post("/api/matrix")
        if r.status_code == 503:
            raise Check("Podman API not connected (see T03)")
        expect(r.status_code == 200, f"HTTP {r.status_code}: {r.text[:150]}")
        m = r.json()
        errors = [row["source"] for row in m["rows"] if row["error"]]
        expect(not errors, f"probe errors from: {errors}")
        cell = {(row["source"], c["target"]): c for row in m["rows"] for c in row["cells"]}
        expect(cell[("demo-web", "demo-api:8000")]["status"] == "ok", "demo-web cannot reach demo-api")
        expect(cell[("demo-web", "demo-db:5432")]["status"] == "dns_fail", "demo-web should NOT reach demo-db")
        expect(all(c["status"] != "ok" for row in m["rows"] if row["source"] == "demo-vault" for c in row["cells"]),
               "demo-vault should reach nothing")
        unexpected = [f for f in m["findings"] if not f["target"].startswith("demo-worker:9000")]
        expect(not unexpected, f"unexpected findings: {[f['message'] for f in unexpected][:2]}")
        self.cache["worker_bug"] = any(f["target"].startswith("demo-worker:9000") for f in m["findings"])
        return m["summary"] + (" (planted demo-worker bug detected)" if self.cache["worker_bug"] else " (demo-worker fixed)")

    def _manage(self, name, action, want):
        r = self.post(f"/api/containers/{name}/{action}")
        if r.status_code == 503:
            raise Check("Podman API not connected (see T03)")
        expect(r.status_code == 200, f"{action} {name}: HTTP {r.status_code} {r.text[:100]}")
        d = r.json()
        expect(d["status_after"] == want, f"{action} {name}: status {d['status_after']}, expected {want}")
        return d

    def t_lifecycle(self):
        self._manage("demo-api", "stop", "exited")
        self._manage("demo-api", "start", "running")
        self._manage("demo-api", "pause", "paused")
        self._manage("demo-api", "unpause", "running")
        self._manage("demo-cache", "restart", "running")
        time.sleep(1.5)
        r = self.tool("scan", {"host": "demo-api", "ports": "8000"})
        expect(r["open"], "demo-api did not come back on port 8000 after restart")
        return "stop -> start -> pause -> unpause -> restart all OK, service back up"

    def t_self_protect(self):
        r = self.post("/api/containers/netscope/stop")
        if r.status_code == 503:
            raise Check("Podman API not connected (see T03)")
        expect(r.status_code == 400, f"stopping itself returned {r.status_code}")
        return "refuses to stop its own container"

    def t_logs(self):
        d = self.api_json("/api/containers/netscope/logs", params={"tail": 20})
        expect(d["logs"].strip(), "no logs returned")
        return f"{d['lines']} log lines"

    def t_inspect_top(self):
        d = self.api_json("/api/containers/demo-db/inspect")
        expect(d.get("State", {}).get("Running"), "demo-db inspect: not running")
        t = self.api_json("/api/containers/demo-db/top")
        expect(t.get("Processes"), "no processes from top")
        return f"inspect OK (PID {d['State'].get('Pid')}), top shows {len(t['Processes'])} process(es)"

    def t_stats(self):
        s = self.api_json("/api/stats")
        names = {x["name"] for x in s if "error" not in x}
        expect(set(EXPECTED) <= names, f"stats missing for {set(EXPECTED) - names}")
        return f"network counters for {len(names)} containers"

    def t_history(self):
        h = self.get("/api/history", params={"limit": 200}).json()
        tools = {x["tool"] for x in h}
        expect({"dns", "ping", "scan", "matrix", "manage"} <= tools, f"history missing tools: {tools}")
        first = self.get(f"/api/history/{h[0]['id']}")
        expect(first.status_code == 200, "cannot read a stored run")
        return f"{len(h)} runs stored in SQLite on the volume"

    def t_report(self):
        r = self.post("/api/report/save")
        expect(r.status_code == 200, f"HTTP {r.status_code}: {r.text[:120]}")
        path = r.json()["saved"]
        expect(os.path.exists(path), f"{path} not written")
        return f"{r.json()['file']} written to bind mount"

    # ---------------------------------------------------------------- runner
    PLAN = [
        ("T01", "Application", "Web server health check", "t_health"),
        ("T02", "Application", "Dashboard, static files and API docs served", "t_dashboard"),
        ("T03", "Podman", "Connected to the Podman API socket", "t_engine"),
        ("T04", "Podman", "Self-detection of the netscope container", "t_self"),
        ("T05", "Podman", "All 7 containers discovered and running", "t_discovery"),
        ("T06", "Networking", "Podman networks exist with correct internal flags", "t_networks"),
        ("T07", "Networking", "Containers attached to the right networks", "t_membership"),
        ("T08", "Networking", "Port mapping host -> container 8080", "t_ports"),
        ("T09", "Storage", "Volume on /data and bind mount on /reports", "t_storage"),
        ("T10", "Networking", "Interfaces per network + container DNS detected", "t_interfaces"),
        ("T11", "Diagnostics", "DNS: container on shared network resolves", "t_dns_ok"),
        ("T12", "Diagnostics", "DNS: isolated container does NOT resolve", "t_dns_isolated"),
        ("T13", "Diagnostics", "Ping demo-web", "t_ping"),
        ("T14", "Diagnostics", "TCP latency to demo-cache:6379", "t_latency"),
        ("T15", "Diagnostics", "Port scan demo-api finds only port 8000", "t_scan"),
        ("T16", "Diagnostics", "HTTP check http://demo-web returns 200", "t_http"),
        ("T17", "Diagnostics", "Traceroute to demo-api", "t_trace"),
        ("T18", "Security", "Invalid / malicious input rejected", "t_validation"),
        ("T19", "Networking", "Connectivity matrix matches the network design", "t_matrix"),
        ("T20", "Management", "Lifecycle: stop/start/pause/unpause/restart", "t_lifecycle"),
        ("T21", "Management", "NetScope refuses to stop itself", "t_self_protect"),
        ("T22", "Troubleshooting", "Container logs", "t_logs"),
        ("T23", "Troubleshooting", "Container inspect + top", "t_inspect_top"),
        ("T24", "Troubleshooting", "Live network stats for all containers", "t_stats"),
        ("T25", "Storage", "Every run is stored in history (SQLite on volume)", "t_history"),
        ("T26", "Storage", "Report saved to the bind-mounted folder", "t_report"),
    ]

    def run_all(self) -> int:
        print(f"{BOLD}NetScope end-to-end self-test  ->  {self.base}{RESET}")
        print(f"{'':2}{'ID':<4} {'RESULT':<6} {'TEST':<58} DETAIL")
        t0 = time.perf_counter()
        for tid, area, name, fn in self.PLAN:
            self.run(tid, area, name, getattr(self, fn))
        passed = sum(r["status"] == "PASS" for r in self.results)
        total = len(self.results)
        secs = round(time.perf_counter() - t0, 1)
        color = GREEN if passed == total else RED
        print(f"\n{color}{BOLD}{passed}/{total} tests passed{RESET} in {secs} s")
        self.summary = {"generated": datetime.now().isoformat(timespec="seconds"), "base": self.base,
                        "passed": passed, "total": total, "seconds": secs, "results": self.results}
        return 0 if passed == total else 1

    def save(self) -> str | None:
        folder = os.environ.get("NETSCOPE_REPORTS_DIR", "/reports")
        try:
            os.makedirs(folder, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = os.path.join(folder, f"selftest-{stamp}.json")
            with open(path, "w") as fh:
                json.dump(self.summary, fh, indent=2)
            with open(os.path.join(folder, f"selftest-{stamp}.txt"), "w") as fh:
                fh.write(f"NetScope self-test {self.summary['generated']}: "
                         f"{self.summary['passed']}/{self.summary['total']} passed in {self.summary['seconds']} s\n\n")
                for r in self.results:
                    fh.write(f"{r['id']}  {r['status']}  [{r['area']}] {r['test']}\n      {r['detail']}\n")
            return path
        except OSError:
            return None


def main(base: str = "http://127.0.0.1:8080", save: bool = True, as_json: bool = False) -> int:
    st = SelfTest(base)
    code = st.run_all()
    if save:
        path = st.save()
        if path:
            print(f"Results saved to {path} (and .txt) - visible in the host's reports folder")
    if as_json:
        print(json.dumps(st.summary, indent=2))
    return code
