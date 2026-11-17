"""NetScope web API + dashboard (FastAPI)."""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, diagnostics as diag, engine as dk, store

STATIC = Path(__file__).parent / "static"
REPORTS_DIR = Path(os.environ.get("NETSCOPE_REPORTS_DIR", "/reports"))

app = FastAPI(
    title="NetScope",
    description="Container Network Diagnostics Toolkit",
    version=__version__,
)


@app.exception_handler(diag.DiagnosticError)
async def _bad_input(_, exc: diag.DiagnosticError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(dk.EngineUnavailable)
async def _no_engine(_, exc: dk.EngineUnavailable):
    return JSONResponse(status_code=503, content={"detail": str(exc), "engine_available": False})


@app.exception_handler(dk.ManageError)
async def _manage_error(_, exc: dk.ManageError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def _record(result: dict) -> dict:
    result["run_id"] = store.save(result)
    return result


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class PingReq(BaseModel):
    host: str
    count: int = Field(4, ge=1, le=20)


class LatencyReq(BaseModel):
    host: str
    port: int = Field(80, ge=1, le=65535)
    count: int = Field(10, ge=1, le=50)


class DnsReq(BaseModel):
    name: str
    record_type: str = "A"
    server: str | None = None


class ScanReq(BaseModel):
    host: str
    ports: str = "common"
    timeout: float = Field(1.0, gt=0, le=10)


class HttpReq(BaseModel):
    url: str
    method: str = "GET"
    timeout: float = Field(5.0, gt=0, le=30)
    verify_tls: bool = True


class TraceReq(BaseModel):
    host: str
    max_hops: int = Field(15, ge=1, le=30)


# --------------------------------------------------------------------------- #
# Health / environment
# --------------------------------------------------------------------------- #
@app.get("/api/health")
async def health():
    return {"status": "ok", "version": __version__, "hostname": socket.gethostname()}


@app.get("/api/overview")
async def overview():
    env = await run_in_threadpool(diag.interfaces)
    dstat = await run_in_threadpool(dk.status)
    return {"version": __version__, "environment": env, "engine": dstat,
            "recent": store.recent(limit=8)}


@app.get("/api/interfaces")
async def get_interfaces():
    return await run_in_threadpool(diag.interfaces)


@app.get("/api/sockets")
async def get_sockets():
    return await diag.socket_table()


# --------------------------------------------------------------------------- #
# Container engine discovery (Podman / Docker)
# --------------------------------------------------------------------------- #
@app.get("/api/engine/status")
async def engine_status():
    return await run_in_threadpool(dk.status)


@app.get("/api/containers")
async def containers(all: bool = True):  # noqa: A002 - query param name
    return await run_in_threadpool(dk.list_containers, all)


@app.get("/api/networks")
async def networks():
    return await run_in_threadpool(dk.list_networks)


# --------------------------------------------------------------------------- #
# Container management & troubleshooting
# --------------------------------------------------------------------------- #
@app.get("/api/containers/{name}/logs")
async def container_logs(name: str, tail: int = Query(200, ge=1, le=2000)):
    return await run_in_threadpool(dk.container_logs, name, tail)


@app.get("/api/containers/{name}/inspect")
async def container_inspect(name: str):
    return await run_in_threadpool(dk.container_inspect, name)


@app.get("/api/containers/{name}/top")
async def container_top(name: str):
    return await run_in_threadpool(dk.container_top, name)


@app.post("/api/containers/{name}/{action}")
async def container_manage(name: str, action: str):
    return _record(await run_in_threadpool(dk.manage, name, action))


@app.get("/api/topology")
async def topology():
    return await run_in_threadpool(dk.topology)


@app.get("/api/stats")
async def stats():
    return await run_in_threadpool(dk.network_stats)


@app.post("/api/matrix")
async def matrix(timeout: float = Query(2.0, gt=0, le=10)):
    result = await run_in_threadpool(dk.connectivity_matrix, timeout)
    ok_cells = sum(1 for r in result["rows"] for c in r["cells"] if c.get("status") == "ok")
    total = sum(len(r["cells"]) for r in result["rows"])
    result.update(tool="matrix", target=f"{len(result['containers'])} containers",
                  summary=f"{ok_cells}/{total} paths reachable, {len(result['findings'])} findings")
    return _record(result)


# --------------------------------------------------------------------------- #
# Diagnostic tools
# --------------------------------------------------------------------------- #
@app.post("/api/tools/ping")
async def tool_ping(req: PingReq):
    return _record(await diag.ping(req.host, req.count))


@app.post("/api/tools/latency")
async def tool_latency(req: LatencyReq):
    return _record(await diag.tcp_latency(req.host, req.port, req.count))


@app.post("/api/tools/dns")
async def tool_dns(req: DnsReq):
    return _record(await diag.dns_lookup(req.name, req.record_type, req.server or None))


@app.post("/api/tools/scan")
async def tool_scan(req: ScanReq):
    return _record(await diag.port_scan(req.host, req.ports, req.timeout))


@app.post("/api/tools/http")
async def tool_http(req: HttpReq):
    return _record(await diag.http_check(req.url, req.method, req.timeout, req.verify_tls))


@app.post("/api/tools/traceroute")
async def tool_trace(req: TraceReq):
    return _record(await diag.traceroute(req.host, req.max_hops))


# --------------------------------------------------------------------------- #
# History & reports
# --------------------------------------------------------------------------- #
@app.get("/api/history")
async def history(limit: int = 50, tool: str | None = None):
    return store.recent(limit, tool)


@app.get("/api/history/{run_id}")
async def history_item(run_id: int):
    item = store.get(run_id)
    if item is None:
        raise HTTPException(404, "run not found")
    return item


@app.delete("/api/history")
async def history_clear():
    return {"deleted": store.clear()}


async def _build_report() -> dict:
    env = await run_in_threadpool(diag.interfaces)
    rep: dict = {"generated": diag.now_iso(), "netscope_version": __version__, "environment": env}
    dstat = await run_in_threadpool(dk.status)
    rep["engine"] = dstat
    if dstat.get("available"):
        rep["containers"] = await run_in_threadpool(dk.list_containers, True)
        rep["networks"] = await run_in_threadpool(dk.list_networks)
    rep["recent_runs"] = [store.get(r["id"]) for r in store.recent(limit=25)]
    return rep


@app.get("/api/report")
async def report():
    """Full JSON snapshot: environment, container inventory and recent results."""
    return JSONResponse(await _build_report(),
                        headers={"Content-Disposition": "attachment; filename=netscope-report.json"})


@app.post("/api/report/save")
async def report_save():
    """Write the report into /reports - a bind mount, so it appears on the host."""
    rep = await _build_report()
    name = f"netscope-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        (REPORTS_DIR / name).write_text(json.dumps(rep, indent=2, default=str))
    except OSError as exc:
        raise HTTPException(500, f"Cannot write to {REPORTS_DIR}: {exc}. Mount a host folder with -v ./reports:/reports")
    files = sorted(p.name for p in REPORTS_DIR.glob("netscope-report-*.json"))
    return {"saved": str(REPORTS_DIR / name), "file": name, "reports_in_folder": len(files)}


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")
