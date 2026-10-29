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
# LinuxServer's own rebuilds. Upgrade the affected packages explicitly here
# instead of waiting on LinuxServer's next dated tag:
#  - c-ares 1.34.6-r0: CVE-2026-33630 (use-after-free/double-free in
#    query-completion handling); 1.34.8-r0 fixes it.
#  - jq 1.8.1-r0: CVE-2026-32316 / CVE-2026-40164 (DoS / potential code
#    execution, hash-collision DoS); 1.8.2-r0 fixes both.
#  - libssl3/libcrypto3 3.5.7-r0: CVE-2026-14456 (OpenSSL QUIC server
#    unbounded memory growth DoS); 3.5.8-r0 fixes it.
#  - curl/libcurl 8.21.0-r0: CVE-2026-13608, -18924, -19931, -80229,
#    -80230, -80231, -80255, -80256, -82208, -82209 (10 CVEs flagged by
#    the CodeQL/Trivy container scan, 2026-09-09); 8.22.0-r0 fixes all.
# All found by the CI Trivy gate (job-squire-audit-2026-09-08.md follow-up
# plus the 2026-09-09 code-scanning re-scan); fixed versions confirmed
# already in Alpine 3.24's repo index at the time of this change.
# Added 2026-10-29 (not yet confirmed against the repo index, which was
# unreachable from the session that made this change; --upgrade takes
# whatever the index has, and the Trivy gate will flag anything still open):
#  - brotli-libs (pulled in by curl): CVE-2026-111497 ("Node2Shell"/
#    "Brotli2Shell", out-of-bounds write in Google Brotli's decoder, on CISA
#    KEV 2026-10-27); fixed upstream in 1.2.1 / 1.1.1 / 1.0.10.
#  - ca-certificates / ca-certificates-bundle: the October 2026 eMudhra
#    intermediate-key compromise. Python's ssl.create_default_context() (SMTP
#    in app/notify.py) uses this system bundle, and OpenSSL never consults
#    browser revocation lists, so the distrusted roots have to be gone from
#    the bundle itself. scripts/check_distrusted_roots.py (run after the pip
#    install below) enforces that, stripping them if Alpine's package still
#    carries them.
#  - musl / musl-utils 1.2.6-r2: CVE-2026-107891 (Trivy severity UNKNOWN,
#    code-scanning alert #302, 2026-10-29); 1.2.6-r3 fixes it. Upgraded as a
#    pair since both come from the same musl source package.
RUN apk add --no-cache --upgrade c-ares jq libssl3 libcrypto3 curl libcurl \
        brotli-libs ca-certificates ca-certificates-bundle musl musl-utils

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
#
# Only pip is upgraded here. This line used to also install `setuptools` and
# `msgpack` (2026-09-08), after the Trivy gate flagged setuptools 70.3.0 and
# msgpack 1.1.2. Both turned out to be pip's OWN vendored copies
# (pip/_vendor, see .trivyignore), not venv packages: on Python 3.12+
# `ensurepip` no longer installs setuptools at all, and nothing in
# requirements.txt depends on either (confirmed 2026-10-29 by resolving it
# alone: 60 packages, neither among them; the test suite passes without
# setuptools). Installing them only added two unused packages to the image.
#
# python3 is installed with --upgrade so a rebuild always takes the newest
# Alpine 3.24 build (CVE-2026-115557, reported fixed in 3.14.10-r0) even if
# a future base tag starts shipping an older python3 of its own.
RUN apk add --no-cache --upgrade python3 py3-pip && \
    python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip

# Install dependencies first for better layer caching. requirements.txt pins
# pydantic/pydantic-core explicitly (transitive via mcp) so this resolution
# is deterministic; the full lockfile has been verified to resolve to binary
# musllinux wheels only, with no source builds, on this base.
#
# mcp 2.x's new/raised transitive deps (httpx2, mcp-types, opentelemetry-api,
# jsonschema, sse-starlette>=3, pyjwt[crypto], python-multipart,
# typing-extensions/typing-inspection) were checked individually on PyPI
# before this bump: every one of them ships only a `py3-none-any` wheel, so
# none of them is a compiled-extension / musl-wheel risk at all -- no
# build-toolchain stage needed here for this migration.
COPY requirements.txt .
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# Fail the build if certifi (requests' CA bundle, pinned in requirements.txt)
# still trusts a distrusted root, and strip any from the system bundle used
# by SMTP. Bind-mounted rather than COPY'd so the script never lands in the
# image. See the script's docstring for the eMudhra background.
RUN --mount=type=bind,source=scripts/check_distrusted_roots.py,target=/tmp/check_distrusted_roots.py \
    /opt/venv/bin/python /tmp/check_distrusted_roots.py --strip-system

# App code.
COPY wsgi.py .
COPY app ./app
# User guide and wiki rendered by the in-app /guide and /wiki/* pages.
COPY docs/Job_Squire_User_Guide.md ./docs/
COPY docs/wiki ./docs/wiki

# BUILD_VERSION default below should track the semantic version in ./VERSION;
# CI overrides it with "<VERSION>-<short sha>" on every publish (see
# .github/workflows/ci.yml).
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
