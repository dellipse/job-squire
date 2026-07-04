FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY wsgi.py .
COPY app ./app
# User guide and wiki rendered by the in-app /guide and /wiki/* pages.
COPY docs/Job_Squire_User_Guide.md ./docs/
COPY docs/wiki ./docs/wiki

# Non-root user. Override at build time with --build-arg PUID=… --build-arg PGID=…
# or set PUID/PGID in data/.env so docker-compose picks them up automatically.
# BUILD_VERSION default below should track the semantic version in ./VERSION;
# CI overrides it with "<VERSION>-<short sha>" on every publish (see
# .github/workflows/docker-publish.yml).
ARG BUILD_VERSION=0.1.0-dev
ENV BUILD_VERSION=${BUILD_VERSION}

ARG PUID=1000
ARG PGID=1000
RUN groupadd -g ${PGID} appuser \
    && useradd -u ${PUID} -g ${PGID} --create-home appuser \
    && mkdir -p /data \
    && chown -R ${PUID}:${PGID} /app /data
USER appuser

VOLUME ["/data"]
EXPOSE 8000

# Baseline healthcheck for the default (web) role, so a wedged/crashed container
# is detectable even outside docker-compose (e.g. `docker run` directly, or an
# orchestrator that only reads image-level HEALTHCHECK). docker-compose.yml
# already sets an equivalent check explicitly for clarity; the worker and MCP
# services override this with their own compose-level healthcheck (the worker
# has no HTTP endpoint, and MCP listens on a different port).
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# 2 workers is plenty for two users. Bind to all interfaces inside the container.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", \
     "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
