# Single container running all three Job Squire processes (web, worker, mcp)
# under s6-overlay as PID 1, on the LinuxServer Alpine base. This base has
# no "latest"; the tag below is pinned to a specific dated release, now on
# the Alpine 3.24 line.
#
# Bumped from 3.23-9ba43c66-ls19 on 2026-07-17: the 3.23 line's curl/libcurl
# (8.19.0-r0) had two unfixed HIGH CVEs, CVE-2026-5773 and CVE-2026-6276,
# with no backport landed on that branch. 3.24's main repo carries curl
# 8.21.0-r0, which resolves both. This also moves the base's python3 package
# from 3.12 to 3.14 (see the venv comment below); that jump was re-verified
# before this bump, not assumed -- the full requirements.txt lockfile installs
# as binary musllinux wheels and imports cleanly at runtime under 3.14.
FROM ghcr.io/linuxserver/baseimage-alpine:3.24-03b33b49-ls6

# We are a downstream image, not a LinuxServer first-party one, so their init
# must not overwrite the branding file we ship below.
ENV LSIO_FIRST_PARTY=false

# The base tag above is pinned to a dated LinuxServer release (see comment
# above), so it lags Alpine's rolling package repos for CVE fixes between
# LinuxServer's own rebuilds. c-ares 1.34.6-r0 in this base has
# CVE-2026-33630 (use-after-free/double-free in query-completion handling);
# 1.34.8-r0 fixing it is already in Alpine's v3.23 main repo. Upgrade it
# explicitly here instead of waiting on LinuxServer's next dated tag.
RUN apk add --no-cache --upgrade c-ares

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PATH="/opt/venv/bin:${PATH}"

# LinuxServer PUID/PGID/UMASK convention: the base's init-adduser reassigns
# the existing "abc" account to these IDs at container start (overridable via
# `-e PUID=... -e PGID=...`), rather than us creating our own user at build
# time. Preserve the previous image's build-arg defaults of 1000.
ARG PUID=1000
ARG PGID=1000
ENV PUID=${PUID}
ENV PGID=${PGID}

WORKDIR /app

# This base ships Python 3.14 (the 3.23-line base this image used before
# 2026-07-17 shipped 3.12; see the FROM comment above for why we moved).
# The app runs unchanged on 3.14. Before bumping, the full requirements.txt
# lockfile (61 packages, including cryptography/lxml/pydantic_core, the
# packages most likely to lack fresh-CPython musllinux wheels) was verified
# to install with `pip install --only-binary=:all:` and to import cleanly
# at runtime under 3.14 on this base, so this isn't an unverified jump.
# Installed into a venv so pip doesn't fight Alpine's PEP 668
# externally-managed system Python.
#
# The venv's own bootstrapped pip lags behind Alpine's apk package and has
# had several CVEs (path traversal / arbitrary file overwrite via malicious
# wheel installs, e.g. CVE-2026-8643, CVE-2026-6357, CVE-2025-8869). pip
# itself isn't invoked at runtime by the running app, but it stays present
# in the shipped image, so upgrade it explicitly rather than leaving
# whatever `ensurepip` bundled.
RUN apk add --no-cache python3 py3-pip && \
    python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip

# Install dependencies first for better layer caching. requirements.txt pins
# pydantic/pydantic-core explicitly (transitive via mcp) so this resolution
# is deterministic; the full lockfile has been verified to resolve to binary
# musllinux wheels only, with no source builds, on this base.
COPY requirements.txt .
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# App code.
COPY wsgi.py .
COPY app ./app
# User guide and wiki rendered by the in-app /guide and /wiki/* pages.
COPY docs/Job_Squire_User_Guide.md ./docs/
COPY docs/wiki ./docs/wiki

# BUILD_VERSION default below should track the semantic version in ./VERSION;
# CI overrides it with "<VERSION>-<short sha>" on every publish (see
# .github/workflows/docker-publish.yml).
ARG BUILD_VERSION=0.1.0-dev
ENV BUILD_VERSION=${BUILD_VERSION}

# /app is one of the base's well-known dirs; its own init-adduser already
# re-chowns it to abc on every boot, and the copied source is world-readable,
# so no explicit chown is needed here.

# s6 service definitions (web, worker, mcp longruns; init-data-dir oneshot;
# the "user" bundle wiring that starts them; and our branding banner).
COPY root/ /

RUN find /etc/s6-overlay/s6-rc.d -type f \( -name run -o -name up \) -exec chmod +x {} + && \
    chmod +x /etc/s6-overlay/s6-rc.d/web/health-check /etc/s6-overlay/scripts/healthcheck

VOLUME ["/data"]
EXPOSE 8000
EXPOSE 9000

# Aggregated check for all three processes: web's /health, mcp's /health, and
# the worker's heartbeat file (it has no HTTP endpoint of its own). Replaces
# the three separate per-container healthchecks the legacy compose file
# still uses for job-squire / job-squire-worker / job-squire-mcp.
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD ["/etc/s6-overlay/scripts/healthcheck"]
