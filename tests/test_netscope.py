"""Unit + API tests. Run with:  pytest -q   (no Docker needed)."""

import asyncio
import os
import socket
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler

import pytest

os.environ.setdefault("NETSCOPE_DB", "/tmp/netscope-test.db")

from fastapi.testclient import TestClient  # noqa: E402

from netscope import diagnostics as d  # noqa: E402
from netscope.app import app  # noqa: E402
from netscope.cli import main as cli_main  # noqa: E402


@pytest.fixture(scope="module")
def http_server():
    srv = HTTPServer(("127.0.0.1", 0), SimpleHTTPRequestHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ------------------------------------------------------------------ helpers
def test_parse_ports():
    assert d.parse_ports("80,443,8000-8002") == [80, 443, 8000, 8001, 8002]
    assert d.parse_ports("common") == d.COMMON_PORTS
    with pytest.raises(d.DiagnosticError):
        d.parse_ports("0")
    with pytest.raises(d.DiagnosticError):
        d.parse_ports("1-5000")


@pytest.mark.parametrize("bad", ["", "-c 1 evil", "a b", "x;rm -rf /", "$(id)"])
def test_validate_target_rejects(bad):
    with pytest.raises(d.DiagnosticError):
        d.validate_target(bad)


def test_validate_target_accepts():
    for ok in ["demo-web", "10.0.0.1", "example.com", "fe80::1", "my_svc"]:
        assert d.validate_target(ok) == ok


# ------------------------------------------------------------------ diagnostics
def test_tcp_states(http_server):
    open_r = asyncio.run(d.tcp_connect("127.0.0.1", http_server))
    closed_r = asyncio.run(d.tcp_connect("127.0.0.1", free_port()))
    assert open_r["state"] == "open"
    assert closed_r["state"] == "closed"


def test_scan_finds_open_port(http_server):
    r = asyncio.run(d.port_scan("127.0.0.1", f"{http_server - 2}-{http_server + 2}"))
    assert r["ok"] and any(p["port"] == http_server for p in r["open"])


def test_latency(http_server):
    r = asyncio.run(d.tcp_latency("127.0.0.1", http_server, count=3))
    assert r["ok"] and r["received"] == 3 and r["avg_ms"] is not None


def test_http_check(http_server):
    r = asyncio.run(d.http_check(f"http://127.0.0.1:{http_server}/"))
    assert r["ok"] and r["status_code"] == 200


def test_probe_classifies(http_server):
    r = asyncio.run(d.probe_targets([f"127.0.0.1:{http_server}", f"127.0.0.1:{free_port()}",
                                     "no-such-host-netscope.invalid:80"]))
    statuses = [x["status"] for x in r["results"]]
    assert statuses == ["ok", "refused", "dns_fail"]


def test_interfaces():
    r = d.interfaces()
    assert r["ok"] and any(i["name"] == "lo" for i in r["interfaces"])


# ------------------------------------------------------------------ API
client = TestClient(app)


def test_api_health():
    assert client.get("/api/health").json()["status"] == "ok"


def test_api_dashboard_served():
    r = client.get("/")
    assert r.status_code == 200 and "NetScope" in r.text
    assert client.get("/static/app.js").status_code == 200


def test_api_tool_and_history(http_server):
    r = client.post("/api/tools/scan", json={"host": "127.0.0.1", "ports": str(http_server)})
    assert r.status_code == 200 and r.json()["run_id"]
    run_id = r.json()["run_id"]
    assert client.get(f"/api/history/{run_id}").json()["tool"] == "scan"
    assert any(x["id"] == run_id for x in client.get("/api/history").json())


def test_api_bad_input():
    r = client.post("/api/tools/ping", json={"host": "; rm -rf /"})
    assert r.status_code == 400


def test_api_overview_without_engine_is_graceful():
    r = client.get("/api/overview")
    assert r.status_code == 200
    assert "available" in r.json()["engine"]


def test_report_export():
    r = client.get("/api/report")
    assert r.status_code == 200 and "environment" in r.json()


# ------------------------------------------------------------------ CLI
def test_cli_scan(http_server, capsys):
    code = cli_main(["scan", "127.0.0.1", "-p", str(http_server)])
    assert code == 0 and str(http_server) in capsys.readouterr().out


# ------------------------------------------------------------------ Podman-specific
def test_dns_classification():
    import ipaddress
    nets = [ipaddress.IPv4Network("10.89.0.0/24")]
    assert d.classify_dns({"nameservers": ["10.89.0.1"]}, nets)["container_dns"] == "podman"
    assert d.classify_dns({"nameservers": ["127.0.0.11"]}, nets)["container_dns"] == "docker"
    assert d.classify_dns({"nameservers": ["8.8.8.8"]}, nets)["container_dns"] is None


def test_report_save_to_bind_mount(tmp_path, monkeypatch):
    import netscope.app as webapp
    monkeypatch.setattr(webapp, "REPORTS_DIR", tmp_path)
    r = client.post("/api/report/save")
    assert r.status_code == 200
    assert (tmp_path / r.json()["file"]).exists()


def test_manage_without_engine_returns_503(monkeypatch):
    from netscope import engine
    monkeypatch.setattr(engine, "_socket_candidates", lambda: ["unix:///nonexistent/podman.sock"])
    engine._client_cached.cache_clear()
    r = client.post("/api/containers/demo-web/restart")
    engine._client_cached.cache_clear()
    assert r.status_code == 503 and "socket" in r.json()["detail"]
