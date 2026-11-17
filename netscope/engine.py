"""Container engine integration (Podman first, Docker compatible).

NetScope talks to the container engine through its REST API socket:
  * Podman:  podman.socket  ->  /run/podman/podman.sock (rootful)
                                /run/user/<uid>/podman/podman.sock (rootless)
  * Docker:  /var/run/docker.sock

Podman exposes a Docker-compatible API on that socket, so one client library
(docker-py) works with both engines. The socket is mounted into the NetScope
container at /run/podman/podman.sock.

Provides: engine status, container/network discovery, live network stats,
topology, container lifecycle management (logs, inspect, start/stop/restart)
and the container-to-container connectivity matrix.
"""

from __future__ import annotations

import json
import os
import socket
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import psutil

from .diagnostics import now_iso

PROBE_LABEL = "netscope.role"
MANAGE_ACTIONS = ("start", "stop", "restart", "pause", "unpause")

try:
    import docker
    from docker.errors import APIError, DockerException, ImageNotFound, NotFound
except ImportError:  # pragma: no cover - docker-py is in requirements
    docker = None
    DockerException = ImageNotFound = NotFound = APIError = Exception


class EngineUnavailable(RuntimeError):
    pass


class ManageError(ValueError):
    pass


def _socket_candidates() -> list[str]:
    raw = [
        os.environ.get("NETSCOPE_ENGINE_SOCKET"),
        os.environ.get("CONTAINER_HOST"),
        os.environ.get("DOCKER_HOST"),
        "unix:///run/podman/podman.sock",
        f"unix:///run/user/{os.getuid()}/podman/podman.sock",
        "unix:///var/run/docker.sock",
    ]
    out = []
    for s in raw:
        if not s:
            continue
        if s.startswith("/"):
            s = "unix://" + s
        if s not in out:
            out.append(s)
    return out


@lru_cache(maxsize=1)
def _client_cached():
    if docker is None:
        raise EngineUnavailable("docker-py (Podman/Docker API client) not installed")
    errors = []
    for url in _socket_candidates():
        if url.startswith("unix://") and not os.path.exists(url[len("unix://"):]):
            errors.append(f"{url}: not found")
            continue
        try:
            c = docker.DockerClient(base_url=url, timeout=15)
            c.ping()
            c.netscope_url = url
            return c
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {str(exc)[:80]}")
    raise EngineUnavailable(
        "Cannot reach the Podman/Docker API socket. Start NetScope with "
        "'-v <podman socket>:/run/podman/podman.sock --security-opt label=disable' "
        "(see README). Tried: " + "; ".join(errors)
    )


def client():
    try:
        return _client_cached()
    except EngineUnavailable:
        _client_cached.cache_clear()
        raise


@lru_cache(maxsize=1)
def _engine_identity() -> tuple[str, str, str]:
    c = client()
    ver = c.version()
    blob = json.dumps(ver).lower()
    name = "Podman" if "podman" in blob else "Docker"
    version = ver.get("Version", "")
    for comp in ver.get("Components") or []:
        if "podman" in (comp.get("Name") or "").lower():
            version = comp.get("Version") or version
    return name, version, ver.get("ApiVersion", "")


def status() -> dict:
    try:
        c = client()
        info = c.info()
        name, version, api = _engine_identity()
        me = self_container()
        return {
            "available": True,
            "engine": name,
            "server_version": version or info.get("ServerVersion"),
            "api_version": api,
            "socket": getattr(c, "netscope_url", None),
            "os": info.get("OperatingSystem"),
            "rootless": _is_rootless(info),
            "containers_running": info.get("ContainersRunning"),
            "containers_total": info.get("Containers"),
            "images": info.get("Images"),
            "self_container": me.name if me else None,
        }
    except EngineUnavailable as exc:
        return {"available": False, "error": str(exc)}


def _is_rootless(info: dict):
    for opt in info.get("SecurityOptions") or []:
        if "rootless" in str(opt):
            return True
    return None


# --------------------------------------------------------------------------- #
# Self detection (which container is NetScope running in?)
# --------------------------------------------------------------------------- #
def _local_ipv4() -> set[str]:
    ips = set()
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith("127."):
                ips.add(a.address)
    return ips


_self_id: str | None = None


def self_container():
    """Find our own container: by hostname (= short id by default), else by IP."""
    global _self_id
    c = client()
    if _self_id:
        try:
            return c.containers.get(_self_id)
        except Exception:  # noqa: BLE001
            _self_id = None
    try:
        me = c.containers.get(socket.gethostname())
        _self_id = me.id
        return me
    except Exception:  # noqa: BLE001
        pass
    mine = _local_ipv4()
    if not mine:
        return None
    for cont in c.containers.list():
        nets = cont.attrs.get("NetworkSettings", {}).get("Networks") or {}
        if any((v.get("IPAddress") or "") in mine for v in nets.values()):
            _self_id = cont.id
            return cont
    return None


def probe_image() -> str:
    """Image used for matrix probe sidecars: the image NetScope itself runs from."""
    me = None
    try:
        me = self_container()
    except Exception:  # noqa: BLE001
        pass
    if me is not None:
        return me.attrs.get("Image") or me.attrs.get("Config", {}).get("Image")
    return os.environ.get("NETSCOPE_IMAGE", "localhost/netscope:latest")


def _probe_image_name() -> str:
    try:
        me = self_container()
        if me is not None:
            return me.attrs.get("Config", {}).get("Image") or me.attrs.get("ImageName") or "self"
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("NETSCOPE_IMAGE", "localhost/netscope:latest")


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def _is_probe(c) -> bool:
    return (c.labels or {}).get(PROBE_LABEL) == "probe"


def _exposed_ports(attrs: dict) -> list[int]:
    ports = set()
    for key in (attrs.get("Config", {}).get("ExposedPorts") or {}):
        num, _, proto = key.partition("/")
        if proto in ("", "tcp") and num.isdigit():
            ports.add(int(num))
    return sorted(ports)


def _published_ports(attrs: dict) -> list[dict]:
    out = []
    for key, bindings in (attrs.get("NetworkSettings", {}).get("Ports") or {}).items():
        for b in bindings or []:
            out.append({"container": key, "host_ip": b.get("HostIp") or "0.0.0.0", "host_port": b.get("HostPort")})
    return out


def _mounts(attrs: dict) -> list[dict]:
    out = []
    for m in attrs.get("Mounts") or []:
        out.append({"type": m.get("Type"), "source": m.get("Name") or m.get("Source"),
                    "destination": m.get("Destination"), "rw": m.get("RW", True)})
    return out


def _container_summary(c, self_id: str | None = None) -> dict:
    a = c.attrs
    nets = a.get("NetworkSettings", {}).get("Networks") or {}
    state = a.get("State", {}) or {}
    health = state.get("Health") or state.get("Healthcheck") or {}
    return {
        "id": c.id[:12],
        "name": c.name.lstrip("/"),
        "image": (a.get("Config", {}).get("Image") or a.get("ImageName") or ""),
        "status": c.status,
        "health": health.get("Status") or None,
        "started_at": state.get("StartedAt"),
        "restart_count": a.get("RestartCount", 0),
        "network_mode": a.get("HostConfig", {}).get("NetworkMode"),
        "networks": [
            {
                "name": n,
                "ip": v.get("IPAddress") or None,
                "gateway": v.get("Gateway") or None,
                "mac": v.get("MacAddress") or None,
                "aliases": [x for x in (v.get("Aliases") or []) if x != c.id[:12]],
            }
            for n, v in nets.items()
        ],
        "exposed_ports": _exposed_ports(a),
        "published_ports": _published_ports(a),
        "mounts": _mounts(a),
        "is_self": bool(self_id and c.id == self_id),
        "compose_service": (c.labels or {}).get("com.docker.compose.service"),
        "compose_project": (c.labels or {}).get("com.docker.compose.project"),
    }


def _self_id_safe() -> str | None:
    try:
        me = self_container()
        return me.id if me else None
    except Exception:  # noqa: BLE001
        return None


def list_containers(all_containers: bool = True) -> list[dict]:
    me = _self_id_safe()
    cs = client().containers.list(all=all_containers)
    return [_container_summary(c, me) for c in cs if not _is_probe(c)]


def list_networks() -> list[dict]:
    out = []
    for n in client().networks.list():
        try:
            n.reload()
        except Exception:  # noqa: BLE001
            pass
        a = n.attrs
        ipam = (a.get("IPAM") or {}).get("Config") or []
        members = []
        for cid, info in (a.get("Containers") or {}).items():
            name = (info.get("Name") or "").lstrip("/")
            if name.startswith("netscope-probe-"):
                continue
            members.append({"id": cid[:12], "name": name,
                            "ipv4": (info.get("IPv4Address") or "").split("/")[0] or None})
        out.append({
            "id": n.id[:12],
            "name": n.name,
            "driver": a.get("Driver"),
            "scope": a.get("Scope"),
            "internal": a.get("Internal", False),
            "dns_enabled": (a.get("Options") or {}).get("dns_enabled") if "Options" in a else None,
            "subnets": [c.get("Subnet") for c in ipam if c.get("Subnet")],
            "gateways": [c.get("Gateway") for c in ipam if c.get("Gateway")],
            "containers": sorted(members, key=lambda m: m["name"] or ""),
            "compose_project": (a.get("Labels") or {}).get("com.docker.compose.project"),
        })
    return sorted(out, key=lambda n: (n["driver"] != "bridge", n["name"]))


def topology() -> dict:
    """Graph of networks <-> containers."""
    containers = list_containers(all_containers=False)
    networks = [n for n in list_networks() if n["name"] not in ("none",)]
    edges = []
    for c in containers:
        for net in c["networks"]:
            edges.append({"container": c["name"], "network": net["name"], "ip": net["ip"]})
    used = {e["network"] for e in edges}
    return {
        "timestamp": now_iso(),
        "containers": [{"name": c["name"], "image": c["image"], "status": c["status"], "is_self": c["is_self"],
                        "ports": c["exposed_ports"], "network_mode": c["network_mode"]} for c in containers],
        "networks": [{"name": n["name"], "driver": n["driver"], "internal": n["internal"],
                      "subnets": n["subnets"]} for n in networks if n["name"] in used],
        "edges": edges,
    }


# --------------------------------------------------------------------------- #
# Live per-container network counters
# --------------------------------------------------------------------------- #
def _stats_one(c) -> dict:
    try:
        try:
            s = c.stats(stream=False, one_shot=True)
        except TypeError:
            s = c.stats(stream=False)
        nets = s.get("networks") or {}
        keys = ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors", "tx_errors", "rx_dropped", "tx_dropped")
        out = {"name": c.name.lstrip("/")}
        for k in keys:
            out[k] = sum((v or {}).get(k, 0) or 0 for v in nets.values())
        return out
    except Exception as exc:  # noqa: BLE001
        return {"name": c.name.lstrip("/"), "error": str(exc)[:200]}


def network_stats() -> list[dict]:
    cs = [c for c in client().containers.list() if not _is_probe(c)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(_stats_one, cs))


# --------------------------------------------------------------------------- #
# Container management (lifecycle + troubleshooting)
# --------------------------------------------------------------------------- #
def _get(name: str):
    try:
        return client().containers.get(name)
    except NotFound as exc:
        raise ManageError(f"No such container: {name}") from exc


def container_logs(name: str, tail: int = 200) -> dict:
    c = _get(name)
    tail = max(1, min(int(tail), 2000))
    raw = c.logs(tail=tail, timestamps=True, stdout=True, stderr=True)
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
    return {"tool": "logs", "target": c.name.lstrip("/"), "timestamp": now_iso(), "tail": tail,
            "lines": text.count("\n"), "logs": text, "ok": True,
            "summary": f"logs {c.name.lstrip('/')}: last {text.count(chr(10))} lines"}


def container_inspect(name: str) -> dict:
    c = _get(name)
    return c.attrs


def container_top(name: str) -> dict:
    c = _get(name)
    try:
        return c.top()
    except APIError as exc:
        raise ManageError(f"top failed: {exc.explanation or exc}") from exc


def manage(name: str, action: str) -> dict:
    if action not in MANAGE_ACTIONS:
        raise ManageError(f"Action must be one of {MANAGE_ACTIONS}")
    c = _get(name)
    me = _self_id_safe()
    if me and c.id == me and action in ("stop", "restart", "pause"):
        raise ManageError("NetScope will not stop/restart/pause its own container - use 'podman stop netscope' instead")
    before = c.status
    try:
        if action in ("stop", "restart"):
            getattr(c, action)(timeout=5)
        else:
            getattr(c, action)()
    except APIError as exc:
        raise ManageError(f"{action} failed: {exc.explanation or exc}") from exc
    c.reload()
    return {"tool": "manage", "target": c.name.lstrip("/"), "action": action, "timestamp": now_iso(),
            "status_before": before, "status_after": c.status, "ok": True,
            "summary": f"{action} {c.name.lstrip('/')}: {before} -> {c.status}"}


# --------------------------------------------------------------------------- #
# Connectivity matrix (sidecar probes)
# --------------------------------------------------------------------------- #
def _run_probe_from(source, image: str, targets: list[str], timeout: float) -> dict:
    """Start a throw-away NetScope container inside `source`'s network namespace
    (network_mode=container:<id>, the same as `podman run --network container:X`)
    and run `netscope probe` from there."""
    cmd = ["netscope", "probe", "--json", "--timeout", str(timeout), *targets]
    probe = None
    try:
        probe = client().containers.create(
            image,
            cmd,
            network_mode=f"container:{source.id}",
            labels={PROBE_LABEL: "probe"},
            name=f"netscope-probe-{source.name.lstrip('/')}"[:50] + "-" + os.urandom(3).hex(),
            environment={"NETSCOPE_DB": "/tmp/probe.db"},
        )
        probe.start()
        probe.wait(timeout=30 + timeout * 2)
        out = probe.logs(stdout=True, stderr=False)
        out = out.decode() if isinstance(out, bytes) else str(out)
        return json.loads(out.strip().splitlines()[-1])
    except ImageNotFound:
        return {"error": f"Probe image '{image}' not found locally."}
    except (APIError, DockerException, ValueError, IndexError) as exc:
        return {"error": str(exc)[:300]}
    except Exception as exc:  # noqa: BLE001 - e.g. wait() timeout
        return {"error": f"probe failed: {exc}"[:300]}
    finally:
        if probe is not None:
            try:
                probe.remove(v=True, force=True)
            except Exception:  # noqa: BLE001
                pass


def connectivity_matrix(timeout: float = 2.0) -> dict:
    """For every running container, test TCP reachability of every other
    container's exposed ports *from inside that container's network namespace*,
    and compare with the network design (do they share a network?)."""
    c_api = client()
    me = _self_id_safe()
    running = [c for c in c_api.containers.list() if not _is_probe(c)]
    summaries = {c.name.lstrip("/"): _container_summary(c, me) for c in running}
    image = probe_image()

    def nets_of(name: str) -> set[str]:
        s = summaries[name]
        if (s["network_mode"] or "").startswith("container:"):
            return set()
        return {n["name"] for n in s["networks"]} - {"host", "none"}

    targets_by_name = {name: [f"{name}:{p}" for p in s["exposed_ports"][:3]]
                       for name, s in summaries.items() if s["exposed_ports"]}

    def probe_source(src):
        name = src.name.lstrip("/")
        tgts = [t for n, ts in targets_by_name.items() if n != name for t in ts]
        if not tgts:
            return name, {"results": []}
        if (summaries[name]["network_mode"] or "") == "host":
            return name, {"error": "host network mode - skipped"}
        return name, _run_probe_from(src, image, tgts, timeout)

    with ThreadPoolExecutor(max_workers=4) as pool:
        raw = dict(pool.map(probe_source, running))

    rows, findings = [], []
    for src_name, payload in raw.items():
        cells = []
        for r in payload.get("results", []):
            dst = r.get("host")
            shared = sorted(nets_of(src_name) & nets_of(dst)) if dst in summaries else []
            expected = bool(shared)
            actual = r.get("status") == "ok"
            cells.append({**r, "target_name": dst, "shared_networks": shared, "expected": expected,
                          "mismatch": expected != actual})
            if expected and not actual:
                findings.append({
                    "severity": "warning", "source": src_name, "target": r["target"],
                    "message": f"{src_name} shares {', '.join(shared)} with {dst} but "
                               f"{r['target']} is {r.get('status')} (service down or not listening?)",
                })
            elif not expected and actual:
                findings.append({
                    "severity": "info", "source": src_name, "target": r["target"],
                    "message": f"{src_name} reached {r['target']} without a shared network "
                               f"(routed via host/published port?)",
                })
        rows.append({"source": src_name, "error": payload.get("error"), "cells": cells})

    return {
        "timestamp": now_iso(),
        "probe_image": _probe_image_name(),
        "containers": sorted(summaries),
        "targets": targets_by_name,
        "rows": sorted(rows, key=lambda r: r["source"]),
        "findings": findings,
        "ok": not any(r["error"] for r in rows),
    }
