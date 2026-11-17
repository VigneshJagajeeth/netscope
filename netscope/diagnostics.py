"""Core network diagnostics used by both the web API and the CLI.

Every function returns a plain dict so results can be shown in the dashboard,
printed by the CLI, or stored in the history database as JSON.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import shutil
import socket
import ssl
import statistics
import time
from datetime import datetime, timezone
from typing import Iterable

import dns.exception
import dns.resolver
import httpx
import psutil

# Hostnames / IPs only: letters, digits, dots, dashes, underscores, colons (IPv6).
_TARGET_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,253}$")

MAX_PORTS_PER_SCAN = 1024
COMMON_PORTS = [
    21, 22, 25, 53, 80, 110, 143, 443, 465, 587, 993, 995,
    1433, 1521, 2375, 2376, 3000, 3306, 5000, 5432, 5672, 6379,
    8000, 8080, 8081, 8443, 9000, 9090, 9200, 11211, 15672, 27017,
]
SERVICE_NAMES = {
    21: "ftp", 22: "ssh", 25: "smtp", 53: "dns", 80: "http", 110: "pop3", 143: "imap",
    443: "https", 465: "smtps", 587: "submission", 993: "imaps", 995: "pop3s",
    1433: "mssql", 1521: "oracle", 3306: "mysql", 2375: "docker", 2376: "docker-tls", 3000: "dev-http", 5000: "dev-http",
    5432: "postgresql", 5672: "amqp", 6379: "redis", 8000: "http-alt",
    8080: "http-proxy", 8081: "http-alt", 8443: "https-alt", 9000: "http-alt",
    9090: "prometheus", 9200: "elasticsearch", 11211: "memcached",
    15672: "rabbitmq-mgmt", 27017: "mongodb",
}


class DiagnosticError(ValueError):
    """Raised for invalid user input (bad host, bad port range...)."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_target(target: str) -> str:
    target = (target or "").strip()
    if not target or not _TARGET_RE.match(target) or target.startswith("-"):
        raise DiagnosticError(f"Invalid host: {target!r}")
    return target


def parse_ports(spec: str | Iterable[int] | None) -> list[int]:
    """Parse '80,443,8000-8010' or 'common' into a sorted list of ports."""
    if spec is None or spec == "" or spec == "common":
        return list(COMMON_PORTS)
    if not isinstance(spec, str):
        ports = {int(p) for p in spec}
    else:
        ports: set[int] = set()
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo, hi = part.split("-", 1)
                lo_i, hi_i = int(lo), int(hi)
                if lo_i > hi_i:
                    lo_i, hi_i = hi_i, lo_i
                ports.update(range(lo_i, hi_i + 1))
            else:
                ports.add(int(part))
    bad = [p for p in ports if p < 1 or p > 65535]
    if bad:
        raise DiagnosticError(f"Ports out of range: {bad[:5]}")
    if len(ports) > MAX_PORTS_PER_SCAN:
        raise DiagnosticError(f"Too many ports ({len(ports)}); max is {MAX_PORTS_PER_SCAN}")
    return sorted(ports)


def service_name(port: int) -> str:
    if port in SERVICE_NAMES:
        return SERVICE_NAMES[port]
    try:
        return socket.getservbyport(port, "tcp")
    except OSError:
        return ""


async def _run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"timed out after {timeout}s"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def resolve_host(host: str) -> tuple[str | None, float, str | None]:
    """Resolve via the system resolver (Docker's embedded DNS inside containers)."""
    loop = asyncio.get_running_loop()
    start = time.perf_counter()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        ms = (time.perf_counter() - start) * 1000
        return infos[0][4][0], ms, None
    except socket.gaierror as exc:
        ms = (time.perf_counter() - start) * 1000
        return None, ms, str(exc)


async def tcp_connect(host: str, port: int, timeout: float = 2.0) -> dict:
    """Single TCP connect attempt, classified as open / closed / filtered."""
    start = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        ms = (time.perf_counter() - start) * 1000
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 - peer may reset on close
            pass
        return {"port": port, "state": "open", "latency_ms": round(ms, 2)}
    except ConnectionRefusedError:
        return {"port": port, "state": "closed", "latency_ms": None}
    except (asyncio.TimeoutError, TimeoutError):
        return {"port": port, "state": "filtered", "latency_ms": None}
    except OSError as exc:
        return {"port": port, "state": "unreachable", "latency_ms": None, "error": exc.strerror or str(exc)}


# --------------------------------------------------------------------------- #
# Ping (ICMP with TCP fallback)
# --------------------------------------------------------------------------- #
_PING_LINE = re.compile(r"icmp_seq=(\d+).*?ttl=(\d+).*?time=([\d.]+)\s*ms")
_PING_SUMMARY = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")


async def ping(host: str, count: int = 4, timeout: float = 2.0) -> dict:
    host = validate_target(host)
    count = max(1, min(int(count), 20))
    result: dict = {"tool": "ping", "target": host, "timestamp": now_iso()}

    ip, dns_ms, dns_err = await resolve_host(host)
    result["resolved_ip"] = ip
    if ip is None:
        result.update(ok=False, error=f"DNS resolution failed: {dns_err}",
                      summary=f"{host}: DNS resolution failed")
        return result

    ping_bin = shutil.which("ping")
    if not ping_bin:
        tcp = await tcp_latency(host, port=80, count=count, timeout=timeout)
        tcp.update(tool="ping", method="tcp-fallback",
                   note="ping binary not found; measured TCP connect latency to port 80")
        return tcp

    code, out, err = await _run(
        [ping_bin, "-c", str(count), "-W", str(max(1, int(timeout))), "-i", "0.2", ip],
        timeout=count * (timeout + 0.5) + 2,
    )
    replies = [
        {"seq": int(m[0]), "ttl": int(m[1]), "time_ms": float(m[2])}
        for m in _PING_LINE.findall(out)
    ]
    sent, received = count, len(replies)
    m = _PING_SUMMARY.search(out)
    if m:
        sent, received = int(m.group(1)), int(m.group(2))
    times = [r["time_ms"] for r in replies]
    loss = round(100 * (sent - received) / sent, 1) if sent else 100.0
    result.update(
        method="icmp",
        sent=sent,
        received=received,
        loss_pct=loss,
        replies=replies,
        min_ms=round(min(times), 3) if times else None,
        avg_ms=round(statistics.mean(times), 3) if times else None,
        max_ms=round(max(times), 3) if times else None,
        jitter_ms=round(statistics.pstdev(times), 3) if len(times) > 1 else 0.0 if times else None,
        ok=received > 0,
    )
    if not replies and ("Operation not permitted" in err or "permission" in err.lower()):
        result["error"] = "ICMP not permitted - add 'cap_add: [NET_RAW]' to the container"
    elif not replies:
        result["error"] = (err.strip() or "No ICMP replies (host down or ICMP blocked)")[:300]
    result["summary"] = (
        f"{host} ({ip}): {received}/{sent} replies, {loss}% loss"
        + (f", avg {result['avg_ms']} ms" if times else "")
    )
    return result


# --------------------------------------------------------------------------- #
# TCP latency (connect timing statistics)
# --------------------------------------------------------------------------- #
async def tcp_latency(host: str, port: int = 80, count: int = 5, timeout: float = 2.0) -> dict:
    host = validate_target(host)
    port = int(port)
    if not 1 <= port <= 65535:
        raise DiagnosticError("Port must be 1-65535")
    count = max(1, min(int(count), 50))
    samples, states = [], []
    for _ in range(count):
        r = await tcp_connect(host, port, timeout)
        states.append(r["state"])
        if r["state"] == "open":
            samples.append(r["latency_ms"])
        await asyncio.sleep(0.05)
    ok = bool(samples)
    loss = round(100 * (count - len(samples)) / count, 1)
    res = {
        "tool": "latency",
        "target": f"{host}:{port}",
        "timestamp": now_iso(),
        "method": "tcp-connect",
        "sent": count,
        "received": len(samples),
        "loss_pct": loss,
        "samples_ms": samples,
        "min_ms": round(min(samples), 3) if ok else None,
        "avg_ms": round(statistics.mean(samples), 3) if ok else None,
        "max_ms": round(max(samples), 3) if ok else None,
        "p95_ms": round(sorted(samples)[max(0, int(len(samples) * 0.95) - 1)], 3) if ok else None,
        "jitter_ms": round(statistics.pstdev(samples), 3) if len(samples) > 1 else (0.0 if ok else None),
        "ok": ok,
    }
    if not ok:
        res["error"] = f"No successful connections (last state: {states[-1]})"
    res["summary"] = (
        f"{host}:{port} - {len(samples)}/{count} connects, avg {res['avg_ms']} ms, jitter {res['jitter_ms']} ms"
        if ok else f"{host}:{port} - all connects failed ({states[-1]})"
    )
    return res


# --------------------------------------------------------------------------- #
# DNS
# --------------------------------------------------------------------------- #
DNS_TYPES = ["A", "AAAA", "CNAME", "MX", "NS", "TXT", "SRV", "PTR"]


async def dns_lookup(name: str, record_type: str = "A", server: str | None = None) -> dict:
    name = validate_target(name)
    record_type = (record_type or "A").upper()
    if record_type not in DNS_TYPES:
        raise DiagnosticError(f"Record type must be one of {DNS_TYPES}")
    res: dict = {"tool": "dns", "target": name, "record_type": record_type, "timestamp": now_iso()}

    resolver = dns.resolver.Resolver()
    resolver.lifetime = 4.0
    if server:
        server = validate_target(server)
        resolver.nameservers = [server]
    res["nameservers"] = list(resolver.nameservers)

    query_name = name
    if record_type == "PTR":
        try:
            query_name = ipaddress.ip_address(name).reverse_pointer
        except ValueError as exc:
            raise DiagnosticError("PTR lookups need an IP address") from exc

    def _query():
        start = time.perf_counter()
        try:
            ans = resolver.resolve(query_name, record_type, search=True)
            return [r.to_text() for r in ans], ans.rrset.ttl, (time.perf_counter() - start) * 1000, None
        except dns.resolver.NXDOMAIN:
            return [], None, (time.perf_counter() - start) * 1000, "NXDOMAIN (name does not exist)"
        except dns.resolver.NoAnswer:
            return [], None, (time.perf_counter() - start) * 1000, f"No {record_type} records"
        except dns.exception.Timeout:
            return [], None, (time.perf_counter() - start) * 1000, "DNS query timed out"
        except Exception as exc:  # noqa: BLE001
            return [], None, (time.perf_counter() - start) * 1000, str(exc)

    answers, ttl, ms, err = await asyncio.to_thread(_query)
    sys_ip, sys_ms, sys_err = await resolve_host(name) if record_type in ("A", "AAAA") else (None, None, None)

    res.update(
        answers=answers,
        ttl=ttl,
        query_ms=round(ms, 2),
        system_resolver={"ip": sys_ip, "ms": round(sys_ms, 2) if sys_ms else None, "error": sys_err}
        if record_type in ("A", "AAAA") else None,
        ok=bool(answers),
    )
    if err:
        res["error"] = err
    if not answers and sys_ip:
        # e.g. names only in /etc/hosts - DNS servers don't know them but the OS does
        res["ok"] = True
        res["note"] = f"Not in DNS, but the system resolver (/etc/hosts) maps it to {sys_ip}"
        res["summary"] = f"{name} {record_type}: {sys_ip} via system resolver (/etc/hosts)"
        return res
    res["summary"] = (
        f"{name} {record_type}: {', '.join(answers[:4])}{' ...' if len(answers) > 4 else ''} ({res['query_ms']} ms)"
        if answers else f"{name} {record_type}: {err}"
    )
    return res


# --------------------------------------------------------------------------- #
# Port scan
# --------------------------------------------------------------------------- #
async def port_scan(host: str, ports: str | Iterable[int] | None = "common",
                    timeout: float = 1.0, concurrency: int = 200) -> dict:
    host = validate_target(host)
    port_list = parse_ports(ports)
    ip, _, dns_err = await resolve_host(host)
    res: dict = {"tool": "scan", "target": host, "resolved_ip": ip, "timestamp": now_iso(),
                 "ports_scanned": len(port_list)}
    if ip is None:
        res.update(ok=False, error=f"DNS resolution failed: {dns_err}", open=[],
                   summary=f"{host}: DNS resolution failed")
        return res

    sem = asyncio.Semaphore(max(1, min(concurrency, 500)))

    async def probe(p: int):
        async with sem:
            return await tcp_connect(ip, p, timeout)

    start = time.perf_counter()
    results = await asyncio.gather(*(probe(p) for p in port_list))
    elapsed = time.perf_counter() - start
    open_ports = [dict(r, service=service_name(r["port"])) for r in results if r["state"] == "open"]
    counts = {s: sum(1 for r in results if r["state"] == s) for s in ("open", "closed", "filtered", "unreachable")}
    res.update(
        open=open_ports,
        counts=counts,
        duration_s=round(elapsed, 2),
        ok=True,
        summary=f"{host} ({ip}): {len(open_ports)} open of {len(port_list)} scanned"
        + (f" -> {', '.join(str(p['port']) for p in open_ports[:10])}" if open_ports else ""),
    )
    return res


# --------------------------------------------------------------------------- #
# HTTP check
# --------------------------------------------------------------------------- #
def _tls_info(host: str, port: int, timeout: float) -> dict | None:
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
                not_after = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                subject = dict(x[0] for x in cert.get("subject", ()))
                issuer = dict(x[0] for x in cert.get("issuer", ()))
                return {
                    "version": tls.version(),
                    "cipher": tls.cipher()[0],
                    "subject": subject.get("commonName"),
                    "issuer": issuer.get("organizationName") or issuer.get("commonName"),
                    "expires": not_after.isoformat(),
                    "days_left": (not_after - datetime.now(timezone.utc)).days,
                    "valid": True,
                }
    except ssl.SSLCertVerificationError as exc:
        return {"valid": False, "error": exc.verify_message or str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"valid": False, "error": str(exc)}


async def http_check(url: str, method: str = "GET", timeout: float = 5.0, verify_tls: bool = True) -> dict:
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        url = "http://" + url
    parsed = httpx.URL(url)
    validate_target(parsed.host)
    method = method.upper() if method.upper() in ("GET", "HEAD") else "GET"
    res: dict = {"tool": "http", "target": url, "method": method, "timestamp": now_iso()}

    timings: dict = {}
    ip, dns_ms, dns_err = await resolve_host(parsed.host)
    timings["dns_ms"] = round(dns_ms, 2)
    res["resolved_ip"] = ip
    if ip is None:
        res.update(ok=False, error=f"DNS resolution failed: {dns_err}", timings=timings,
                   summary=f"{url}: DNS resolution failed")
        return res

    try:
        start = time.perf_counter()
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, verify=verify_tls) as client:
            resp = await client.request(method, url, headers={"User-Agent": "NetScope/1.0"})
        timings["total_ms"] = round((time.perf_counter() - start) * 1000, 2)
        res.update(
            status_code=resp.status_code,
            reason=resp.reason_phrase,
            http_version=resp.http_version,
            final_url=str(resp.url),
            redirects=[str(r.url) for r in resp.history],
            headers={k: v for k, v in resp.headers.items()
                     if k.lower() in ("server", "content-type", "content-length", "cache-control",
                                      "x-powered-by", "location", "date", "strict-transport-security")},
            body_bytes=len(resp.content),
            ok=resp.status_code < 400,
        )
    except httpx.ConnectError as exc:
        res.update(ok=False, error=f"Connection failed: {exc}")
    except httpx.TimeoutException:
        res.update(ok=False, error=f"Timed out after {timeout}s")
    except httpx.HTTPError as exc:
        res.update(ok=False, error=str(exc))

    if parsed.scheme == "https":
        res["tls"] = await asyncio.to_thread(_tls_info, parsed.host, parsed.port or 443, timeout)
    res["timings"] = timings
    res["summary"] = (
        f"{method} {url} -> {res['status_code']} {res.get('reason', '')} in {timings.get('total_ms')} ms"
        if "status_code" in res else f"{method} {url} -> {res.get('error')}"
    )
    return res


# --------------------------------------------------------------------------- #
# Traceroute
# --------------------------------------------------------------------------- #
_HOP_RE = re.compile(r"^\s*(\d+)\s+(.*)$")


async def traceroute(host: str, max_hops: int = 15, timeout: float = 1.0) -> dict:
    host = validate_target(host)
    max_hops = max(1, min(int(max_hops), 30))
    res: dict = {"tool": "traceroute", "target": host, "timestamp": now_iso()}
    ip, _, dns_err = await resolve_host(host)
    res["resolved_ip"] = ip
    if ip is None:
        res.update(ok=False, error=f"DNS resolution failed: {dns_err}", hops=[],
                   summary=f"{host}: DNS resolution failed")
        return res

    if shutil.which("traceroute"):
        cmd = ["traceroute", "-n", "-q", "1", "-w", str(max(1, int(timeout))), "-m", str(max_hops), ip]
    elif shutil.which("tracepath"):
        cmd = ["tracepath", "-n", "-m", str(max_hops), ip]
    else:
        res.update(ok=False, hops=[], error="traceroute/tracepath not installed",
                   summary="traceroute not available")
        return res

    code, out, err = await _run(cmd, timeout=max_hops * (timeout + 1) + 5)
    hops = []
    for line in out.splitlines()[1:]:
        m = _HOP_RE.match(line)
        if not m:
            continue
        hop_no, rest = int(m.group(1)), m.group(2)
        if rest.strip().startswith("*") or "no reply" in rest:
            hops.append({"hop": hop_no, "ip": None, "rtt_ms": None})
            continue
        ip_m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F:]{3,})", rest)
        rtt_m = re.search(r"([\d.]+)\s*ms", rest)
        hops.append({"hop": hop_no, "ip": ip_m.group(1) if ip_m else None,
                     "rtt_ms": float(rtt_m.group(1)) if rtt_m else None})
    reached = any(h["ip"] == ip for h in hops)
    res.update(hops=hops, reached=reached, ok=bool(hops), raw=out[-4000:])
    if not hops:
        res["error"] = (err.strip() or "no output")[:300]
    res["summary"] = f"{host} ({ip}): {len(hops)} hops, {'destination reached' if reached else 'destination not reached'}"
    return res


# --------------------------------------------------------------------------- #
# Local network environment (interfaces, routes, DNS config)
# --------------------------------------------------------------------------- #
def _hex_to_ip(h: str) -> str:
    return socket.inet_ntoa(bytes.fromhex(h)[::-1])


def read_routes() -> list[dict]:
    routes = []
    try:
        with open("/proc/net/route") as fh:
            next(fh)
            for line in fh:
                f = line.split()
                if len(f) < 8:
                    continue
                dest, gw, mask = _hex_to_ip(f[1]), _hex_to_ip(f[2]), _hex_to_ip(f[7])
                prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
                routes.append({
                    "destination": "default" if dest == "0.0.0.0" and prefix == 0 else f"{dest}/{prefix}",
                    "gateway": None if gw == "0.0.0.0" else gw,
                    "interface": f[0],
                    "metric": int(f[6]),
                })
    except OSError:
        pass
    return routes


def read_resolv_conf() -> dict:
    conf = {"nameservers": [], "search": [], "options": []}
    try:
        with open("/etc/resolv.conf") as fh:
            for line in fh:
                parts = line.split()
                if not parts or parts[0].startswith("#"):
                    continue
                if parts[0] == "nameserver":
                    conf["nameservers"].append(parts[1])
                elif parts[0] in ("search", "domain"):
                    conf["search"].extend(x for x in parts[1:] if x not in conf["search"])
                elif parts[0] == "options":
                    conf["options"].extend(parts[1:])
    except OSError:
        pass
    return conf


def classify_dns(conf: dict, local_nets: list) -> dict:
    """Identify the container engine's built-in DNS server.

    Docker answers on 127.0.0.11; Podman (netavark) runs aardvark-dns on the
    gateway IP of each user-defined network (e.g. 10.89.0.1)."""
    kind, label = None, "external / host resolver"
    for ns in conf["nameservers"]:
        if ns == "127.0.0.11":
            kind, label = "docker", "Docker embedded DNS (127.0.0.11)"
            break
        try:
            ip = ipaddress.ip_address(ns)
        except ValueError:
            continue
        if any(ip in net for net in local_nets):
            kind, label = "podman", f"Podman aardvark-dns on network gateway ({ns})"
            break
    conf["container_dns"] = kind
    conf["container_dns_label"] = label
    return conf


def interfaces() -> dict:
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    io = psutil.net_io_counters(pernic=True)
    out = []
    for name, addr_list in sorted(addrs.items()):
        st = stats.get(name)
        c = io.get(name)
        out.append({
            "name": name,
            "is_up": st.isup if st else None,
            "mtu": st.mtu if st else None,
            "speed_mbps": st.speed if st else None,
            "mac": next((a.address for a in addr_list if a.family == psutil.AF_LINK), None),
            "ipv4": [f"{a.address}/{ipaddress.IPv4Network(f'0.0.0.0/{a.netmask}').prefixlen}"
                     if a.netmask else a.address for a in addr_list if a.family == socket.AF_INET],
            "ipv6": [a.address.split("%")[0] for a in addr_list if a.family == socket.AF_INET6],
            "rx_bytes": c.bytes_recv if c else 0,
            "tx_bytes": c.bytes_sent if c else 0,
            "rx_errors": c.errin if c else 0,
            "tx_errors": c.errout if c else 0,
            "rx_dropped": c.dropin if c else 0,
            "tx_dropped": c.dropout if c else 0,
        })
    local_nets = []
    for addr_list in addrs.values():
        for a in addr_list:
            if a.family == socket.AF_INET and a.netmask and not a.address.startswith("127."):
                local_nets.append(ipaddress.IPv4Network(f"{a.address}/{a.netmask}", strict=False))
    routes = read_routes()
    return {
        "tool": "interfaces",
        "timestamp": now_iso(),
        "hostname": socket.gethostname(),
        "in_container": in_container(),
        "interfaces": out,
        "routes": routes,
        "dns": classify_dns(read_resolv_conf(), local_nets),
        "ok": True,
        "summary": f"{len(out)} interfaces, {len(routes)} routes",
    }


def in_container() -> str | None:
    """Podman creates /run/.containerenv, Docker creates /.dockerenv."""
    if os.path.exists("/run/.containerenv"):
        return "podman"
    if os.path.exists("/.dockerenv"):
        return "docker"
    return None


async def socket_table() -> dict:
    """Listening sockets and established connection counts inside this namespace."""
    def _collect():
        try:
            conns = psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, PermissionError):
            return None
        listening, established = [], 0
        for c in conns:
            if c.status == psutil.CONN_LISTEN:
                listening.append({"address": c.laddr.ip, "port": c.laddr.port,
                                  "proto": "tcp" if c.type == socket.SOCK_STREAM else "udp"})
            elif c.status == psutil.CONN_ESTABLISHED:
                established += 1
        return {"listening": sorted(listening, key=lambda x: x["port"]), "established": established}

    data = await asyncio.to_thread(_collect)
    return {"tool": "sockets", "timestamp": now_iso(), "ok": data is not None, **(data or {"error": "access denied"})}


# --------------------------------------------------------------------------- #
# Multi-target probe (used by the connectivity matrix sidecar)
# --------------------------------------------------------------------------- #
async def probe_targets(targets: list[str], timeout: float = 2.0) -> dict:
    """Probe 'host:port' targets: DNS resolution first, then a TCP connect."""

    async def one(t: str):
        host, _, port_s = t.rpartition(":")
        if not host or not port_s.isdigit():
            return {"target": t, "status": "invalid"}
        validate_target(host)
        ip, dns_ms, err = await resolve_host(host)
        if ip is None:
            return {"target": t, "host": host, "port": int(port_s), "status": "dns_fail",
                    "detail": "name not resolvable from this container (no shared network?)"}
        r = await tcp_connect(ip, int(port_s), timeout)
        status = {"open": "ok", "closed": "refused", "filtered": "timeout"}.get(r["state"], "unreachable")
        return {"target": t, "host": host, "ip": ip, "port": int(port_s), "status": status,
                "latency_ms": r.get("latency_ms"), "detail": r.get("error")}

    results = await asyncio.gather(*(one(t) for t in targets))
    return {"tool": "probe", "timestamp": now_iso(), "hostname": socket.gethostname(), "results": results}
