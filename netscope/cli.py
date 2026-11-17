"""NetScope command-line interface.

Examples (inside the container):
    netscope ping demo-web
    netscope scan demo-db --ports 5432,6379,8000-8010
    netscope dns demo-cache
    netscope http http://demo-web
    netscope trace 8.8.8.8
    netscope containers
    netscope matrix
    netscope selftest
    netscope logs demo-worker
    netscope manage demo-api restart
    netscope serve --port 8080
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import __version__, diagnostics as diag

GREEN, RED, YELLOW, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"


def _c(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if sys.stdout.isatty() else text


def _table(rows: list[list], headers: list[str]) -> str:
    rows = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    out = [_c(line, BOLD), _c("  ".join("-" * w for w in widths), DIM)]
    out += ["  ".join(v.ljust(w) for v, w in zip(r, widths)) for r in rows]
    return "\n".join(out)


def _status_line(result: dict) -> str:
    mark = _c("OK  ", GREEN) if result.get("ok") else _c("FAIL", RED)
    return f"[{mark}] {result.get('summary', '')}"


def _print_result(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, default=str))
        return
    print(_status_line(result))
    tool = result.get("tool")
    if tool == "ping" and result.get("replies"):
        print(_table([[r["seq"], r["ttl"], r["time_ms"]] for r in result["replies"]], ["seq", "ttl", "time_ms"]))
        print(f"min/avg/max/jitter = {result['min_ms']}/{result['avg_ms']}/{result['max_ms']}/{result['jitter_ms']} ms")
    elif tool == "latency" and result.get("ok"):
        print(f"min {result['min_ms']}  avg {result['avg_ms']}  p95 {result['p95_ms']}  "
              f"max {result['max_ms']}  jitter {result['jitter_ms']} ms  loss {result['loss_pct']}%")
    elif tool == "scan" and result.get("open"):
        print(_table([[p["port"], p["state"], p["service"], p["latency_ms"]] for p in result["open"]],
                     ["port", "state", "service", "latency_ms"]))
        print(_c(f"counts: {result['counts']}  ({result['duration_s']} s)", DIM))
    elif tool == "dns":
        for a in result.get("answers", []):
            print(f"  {a}")
        print(_c(f"nameservers: {', '.join(result.get('nameservers', []))}  ttl={result.get('ttl')}", DIM))
    elif tool == "http" and "status_code" in result:
        for k, v in result.get("headers", {}).items():
            print(f"  {k}: {v}")
        if result.get("tls"):
            print(f"  TLS: {result['tls']}")
    elif tool == "traceroute":
        print(_table([[h["hop"], h["ip"] or "*", h["rtt_ms"]] for h in result.get("hops", [])],
                     ["hop", "ip", "rtt_ms"]))
    if result.get("error"):
        print(_c(f"error: {result['error']}", YELLOW))
    if result.get("note"):
        print(_c(f"note: {result['note']}", DIM))


def _save(result: dict) -> None:
    try:
        from . import store
        store.save(result)
    except Exception:  # noqa: BLE001 - history is best effort for the CLI
        pass


def _run_tool(coro, as_json: bool) -> int:
    try:
        result = asyncio.run(coro)
    except diag.DiagnosticError as exc:
        print(_c(f"error: {exc}", RED), file=sys.stderr)
        return 2
    _save(result)
    _print_result(result, as_json)
    return 0 if result.get("ok") else 1


def _docker_call(fn, *args):
    from . import engine as dk
    try:
        return getattr(dk, fn)(*args)
    except dk.EngineUnavailable as exc:
        print(_c(f"error: {exc}", RED), file=sys.stderr)
        sys.exit(3)
    except dk.ManageError as exc:
        print(_c(f"error: {exc}", RED), file=sys.stderr)
        sys.exit(2)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="netscope", description="NetScope - Container Network Diagnostics Toolkit")
    p.add_argument("--version", action="version", version=f"netscope {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--json", action="store_true", help="print raw JSON")
        return sp

    s = sub.add_parser("serve", help="start the web dashboard + REST API")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)

    sp = add("ping", "ICMP ping a host"); sp.add_argument("host"); sp.add_argument("-c", "--count", type=int, default=4)
    sp = add("latency", "TCP connect latency/jitter to host:port")
    sp.add_argument("host"); sp.add_argument("-p", "--port", type=int, default=80); sp.add_argument("-c", "--count", type=int, default=10)
    sp = add("dns", "DNS lookup"); sp.add_argument("name"); sp.add_argument("-t", "--type", default="A")
    sp.add_argument("-s", "--server", default=None, help="DNS server to query (default: /etc/resolv.conf)")
    sp = add("scan", "TCP port scan"); sp.add_argument("host")
    sp.add_argument("-p", "--ports", default="common", help="e.g. 80,443,8000-8100 or 'common'")
    sp.add_argument("--timeout", type=float, default=1.0)
    sp = add("http", "HTTP(S) endpoint check"); sp.add_argument("url"); sp.add_argument("-X", "--method", default="GET")
    sp.add_argument("-k", "--insecure", action="store_true", help="skip TLS verification")
    sp = add("trace", "traceroute to a host"); sp.add_argument("host"); sp.add_argument("-m", "--max-hops", type=int, default=15)
    add("interfaces", "show interfaces, routes and DNS config of this network namespace")
    add("containers", "list containers and their networks")
    add("networks", "list container networks")
    sp = add("matrix", "container-to-container connectivity matrix")
    sp.add_argument("--timeout", type=float, default=2.0)
    sp = sub.add_parser("probe", help="(internal) probe host:port targets - used by matrix sidecars")
    sp.add_argument("targets", nargs="+"); sp.add_argument("--json", action="store_true")
    sp.add_argument("--timeout", type=float, default=2.0)
    sp = add("history", "show recent diagnostic runs"); sp.add_argument("-n", type=int, default=20)
    add("engine", "show the container engine NetScope is connected to (Podman/Docker)")
    sp = add("logs", "show a container's logs"); sp.add_argument("name"); sp.add_argument("-n", "--tail", type=int, default=50)
    sp = add("manage", "start/stop/restart/pause/unpause a container")
    sp.add_argument("name"); sp.add_argument("action", choices=["start", "stop", "restart", "pause", "unpause"])
    sp = add("selftest", "end-to-end test of the whole running deployment (PASS/FAIL table)")
    sp.add_argument("--base", default="http://127.0.0.1:8080", help="NetScope URL (default: this container)")
    sp.add_argument("--no-save", action="store_true", help="don't write results to /reports")
    sp = add("report", "build a JSON report (environment, containers, networks, history)")
    sp.add_argument("--save", action="store_true", help="write it to $NETSCOPE_REPORTS_DIR (/reports)")

    a = p.parse_args(argv)

    if a.cmd == "serve":
        import uvicorn
        uvicorn.run("netscope.app:app", host=a.host, port=a.port, log_level="info")
        return 0
    if a.cmd == "ping":
        return _run_tool(diag.ping(a.host, a.count), a.json)
    if a.cmd == "latency":
        return _run_tool(diag.tcp_latency(a.host, a.port, a.count), a.json)
    if a.cmd == "dns":
        return _run_tool(diag.dns_lookup(a.name, a.type, a.server), a.json)
    if a.cmd == "scan":
        return _run_tool(diag.port_scan(a.host, a.ports, a.timeout), a.json)
    if a.cmd == "http":
        return _run_tool(diag.http_check(a.url, a.method, verify_tls=not a.insecure), a.json)
    if a.cmd == "trace":
        return _run_tool(diag.traceroute(a.host, a.max_hops), a.json)
    if a.cmd == "probe":
        print(json.dumps(asyncio.run(diag.probe_targets(a.targets, a.timeout)), default=str))
        return 0

    if a.cmd == "interfaces":
        r = diag.interfaces()
        if a.json:
            print(json.dumps(r, indent=2)); return 0
        print(_c(f"hostname: {r['hostname']}   in_container: {r['in_container']}", BOLD))
        print(_table([[i["name"], "up" if i["is_up"] else "down", i["mtu"], ", ".join(i["ipv4"]), i["mac"],
                       i["rx_bytes"], i["tx_bytes"]] for i in r["interfaces"]],
                     ["iface", "state", "mtu", "ipv4", "mac", "rx_bytes", "tx_bytes"]))
        print()
        print(_table([[x["destination"], x["gateway"] or "-", x["interface"]] for x in r["routes"]],
                     ["destination", "gateway", "iface"]))
        d = r["dns"]
        print(f"\nDNS: {', '.join(d['nameservers'])}  ({d['container_dns_label']})")
        return 0

    if a.cmd == "containers":
        cs = _docker_call("list_containers", True)
        if a.json:
            print(json.dumps(cs, indent=2)); return 0
        print(_table([[c["name"], c["status"], c["image"][:30],
                       "; ".join(f"{n['name']}={n['ip'] or '-'}" for n in c["networks"]),
                       ",".join(map(str, c["exposed_ports"]))] for c in cs],
                     ["name", "status", "image", "networks", "ports"]))
        return 0

    if a.cmd == "networks":
        ns = _docker_call("list_networks")
        if a.json:
            print(json.dumps(ns, indent=2)); return 0
        print(_table([[n["name"], n["driver"], "yes" if n["internal"] else "no", ",".join(n["subnets"]),
                       ", ".join(m["name"] for m in n["containers"])] for n in ns],
                     ["network", "driver", "internal", "subnet", "containers"]))
        return 0

    if a.cmd == "matrix":
        m = _docker_call("connectivity_matrix", a.timeout)
        if a.json:
            print(json.dumps(m, indent=2)); return 0
        rows = []
        for r in m["rows"]:
            if r["error"]:
                rows.append([r["source"], "-", _c(r["error"][:60], RED), ""])
            for cell in r["cells"]:
                st = cell["status"]
                col = GREEN if st == "ok" else (RED if cell["mismatch"] else DIM)
                rows.append([r["source"], cell["target"], _c(st, col),
                             "expected" if cell["expected"] else "isolated"])
        print(_table(rows, ["source", "target", "result", "by design"]))
        for f in m["findings"]:
            print(_c(f"[{f['severity']}] {f['message']}", YELLOW))
        return 0

    if a.cmd == "engine":
        st = _docker_call("status")
        if a.json or not st.get("available"):
            print(json.dumps(st, indent=2)); return 0 if st.get("available") else 3
        for k in ("engine", "server_version", "api_version", "socket", "rootless", "os",
                  "containers_running", "containers_total", "images", "self_container"):
            print(f"{k:20} {st.get(k)}")
        return 0

    if a.cmd == "logs":
        r = _docker_call("container_logs", a.name, a.tail)
        print(json.dumps(r, indent=2) if a.json else r["logs"].rstrip())
        return 0

    if a.cmd == "manage":
        r = _docker_call("manage", a.name, a.action)
        _save(r)
        _print_result(r, a.json)
        return 0

    if a.cmd == "selftest":
        from .selftest import main as selftest_main
        return selftest_main(a.base, save=not a.no_save, as_json=a.json)

    if a.cmd == "report":
        import asyncio as _a
        from . import app as webapp
        if a.save:
            r = _a.run(webapp.report_save())
            print(json.dumps(r, indent=2) if a.json else f"saved {r['saved']} ({r['reports_in_folder']} reports in folder)")
        else:
            print(json.dumps(_a.run(webapp._build_report()), indent=2, default=str))
        return 0

    if a.cmd == "history":
        from . import store
        runs = store.recent(a.n)
        if a.json:
            print(json.dumps(runs, indent=2)); return 0
        print(_table([[r["id"], r["ts"], r["tool"], "ok" if r["ok"] else "fail", r["summary"][:70]] for r in runs],
                     ["id", "time", "tool", "result", "summary"]))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
