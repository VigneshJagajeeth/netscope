# =============================================================================
#  NetScope - Container Network Diagnostics Toolkit
#  Containerfile (Podman / Buildah / OCI)
#
#  Build:  podman build --format docker -t localhost/netscope:1.0 -f Containerfile .
#          (--format docker keeps the HEALTHCHECK; plain OCI format drops it)
#  Run:    see scripts/podman-up.bat (Windows) or scripts/podman-up.sh (Linux/macOS)
# =============================================================================

# 1. Base image: official slim Python on Debian. Fully-qualified name so Podman
#    never has to guess the registry (short-name resolution).
FROM docker.io/library/python:3.12-slim

# 2. Build-time argument for image versioning (podman build --build-arg VERSION=1.1)
ARG VERSION=1.0.0

# 3. Image metadata (shown by `podman inspect localhost/netscope:1.0`)
LABEL org.opencontainers.image.title="NetScope" \
      org.opencontainers.image.description="Container Network Diagnostics Toolkit - dashboard, REST API and CLI" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT" \
      io.netscope.course="21CSE216P Linux and Container Technologies"

# 4. Runtime configuration through environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NETSCOPE_DB=/data/netscope.db \
    NETSCOPE_REPORTS_DIR=/reports

# 5. Linux networking tools. ping + traceroute are used by the web UI; the rest
#    are for hands-on troubleshooting with `podman exec -it netscope bash`.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        iputils-ping traceroute iproute2 bind9-dnsutils netcat-openbsd curl tcpdump \
 && rm -rf /var/lib/apt/lists/*

# 6. Working directory for the application
WORKDIR /app

# 7. Install Python dependencies first: this layer is cached and only rebuilt
#    when requirements.txt changes, which makes rebuilds fast.
COPY requirements.txt .
RUN pip install -r requirements.txt

# 8. Copy the application source and install the `netscope` command
COPY pyproject.toml README.md ./
COPY netscope ./netscope
RUN pip install --no-deps . \
 && mkdir -p /data /reports

# 9. Persistent storage: SQLite history lives in /data (mount a Podman volume here)
VOLUME ["/data"]

# 10. The dashboard / REST API listens on 8080 (publish with -p 8080:8080)
EXPOSE 8080

# 11. Health check: `podman healthcheck run netscope` / `podman ps` shows (healthy)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

# 12. Default command: start the web server
CMD ["netscope", "serve", "--host", "0.0.0.0", "--port", "8080"]
