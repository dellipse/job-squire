# Installing on macOS

Two paths are covered here: **Docker / Podman** (recommended when you want the full three-container stack) and **native Python** (the fastest way to get running locally).

Both Intel and Apple Silicon (M-series) Macs are supported.

> **Quickstart:** The install script handles runtime detection, configuration, and startup automatically on macOS:
> ```bash
> curl -fsSL https://raw.githubusercontent.com/dellipse/job-squire/main/install.sh -o install.sh
> bash install.sh
> ```
> Requires Homebrew. If neither Docker nor Podman is installed, the script offers to install Podman (via `brew install podman` + `podman machine`), Docker via Colima, or OrbStack — showing you the exact commands before running anything. To undo a completed install, run `bash uninstall.sh` from the same directory.
>
> Follow the steps below if you prefer to set things up manually, or need more control over the configuration.

---

## Option A: Docker or Podman

### 1. Install a container runtime

**Docker Desktop** is the most straightforward option:

1. Download from https://www.docker.com/products/docker-desktop/
2. Open the `.dmg`, drag to Applications, and launch Docker Desktop.
3. Wait for the whale icon in the menu bar to show "Docker Desktop is running."

**Podman Desktop** is a Docker-compatible alternative:

```bash
brew install podman podman-compose
podman machine init
podman machine start
```

Or download Podman Desktop from https://podman-desktop.io/.

**OrbStack** is a fast, lightweight Docker Desktop alternative built for macOS. It is a drop-in replacement: the standard `docker` and `docker compose` commands work unchanged, so nothing else in this guide needs to change. It starts in seconds and uses far less memory than Docker Desktop, which makes it a strong fit on Apple Silicon.

```bash
brew install --cask orbstack
```

Then launch OrbStack once from Applications so it installs the `docker` and `docker compose` command-line tools. Or download it directly from https://orbstack.dev/download. Requires macOS 14 or later.

> Docker Desktop requires a license for commercial use in larger organizations. OrbStack is free for personal use and requires a paid license for commercial use in larger organizations. Podman is always free.

### 2. Clone the repository

```bash
git clone https://github.com/dellipse/job-squire.git
cd job-squire
```

### 3. Create the data directory and environment file

```bash
mkdir -p data
cp examples/.env.example data/.env
```

Open `data/.env` in an editor (TextEdit, VS Code, nano, etc.) and set the required values:

```bash
# Generate a secure secret key:
python3 -c "import secrets; print(secrets.token_hex(32))"

# Paste the result into data/.env:
SECRET_KEY=<paste here>
ADMIN_PASSWORD=<your password>    # avoid $ characters
```

For local dev, also set:

```
SESSION_COOKIE_SECURE=false
```

> **PUID / PGID on macOS:** Docker Desktop on Mac runs containers inside a Linux VM, so the `PUID`/`PGID` values in `data/.env` control the UID/GID inside that VM, not on your Mac. The defaults (`1000:1000`) work fine for local development. File ownership in the bind-mounted `data/` folder will appear as your Mac user regardless.

### 4. Start the containers

```bash
docker compose up -d
```

Verify that all three services started cleanly:

```bash
docker compose logs job-squire          # gunicorn up, accounts seeded
docker compose logs job-squire-worker   # "scheduler up ..."
docker compose logs job-squire-mcp      # uvicorn on :9000
```

The web app is available at **http://localhost:8080**. Sign in with `admin` and the password you set.

### 5. (Optional) Expose via a reverse proxy

For HTTPS access (required for MCP mode) you can run a local reverse proxy such as Caddy:

```bash
brew install caddy
```

Create a `Caddyfile`:

```
Job Squire.localhost {
    reverse_proxy localhost:8080
}
mcp-squire.localhost {
    reverse_proxy localhost:9000
}
```

Then:

```bash
caddy run
```

Add `Job Squire.localhost` and `mcp-squire.localhost` to `/etc/hosts` if they do not resolve automatically. Caddy issues a locally-trusted certificate automatically.

For a full production deployment (Linux server + SWAG), see [deployment.md](../deployment.md).

### Updating

```bash
cd job-squire
docker compose pull
docker compose up -d --no-deps --force-recreate job-squire job-squire-worker job-squire-mcp
```

---

## Option B: Native Python (no Docker)

This is the fastest way to get the web app running on your Mac for development or light local use.

> **Note:** The DB cross-process locking uses `fcntl`. On macOS, `fcntl` is present but behaves differently than on Linux; the app degrades gracefully for single-process use (running only `wsgi.py` without the worker). Running the worker and the web app simultaneously on macOS is possible but untested for heavy concurrent load.

### Prerequisites

- Python 3.12
- `git` (comes with Xcode Command Line Tools: `xcode-select --install`)

Install Python 3.12 via Homebrew (recommended):

```bash
brew install python@3.12
```

Or download the macOS installer from https://www.python.org/downloads/.

After installation, confirm the version:

```bash
python3.12 --version
```

### 1. Clone the repository

```bash
git clone https://github.com/dellipse/job-squire.git
cd job-squire
```

### 2. Create a virtual environment and install dependencies

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Set environment variables

Export them in your shell for a quick test:

```bash
export SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
export ADMIN_PASSWORD=devpass
export USER_PASSWORD=devpass
export DATA_DIR=./data
export SESSION_COOKIE_SECURE=false
```

To make the configuration persistent, use `data/.env`:

```bash
mkdir -p data
cp examples/.env.example data/.env
# Edit data/.env, then load it:
set -a && source data/.env && set +a
```

### 4. Create the data directory and start the app

```bash
mkdir -p data
python wsgi.py
# Web app at http://localhost:8000
```

### 5. (Optional) Run the worker and MCP server

In separate terminals (with the virtual environment activated and env vars set):

```bash
# Automated search scheduler
python -m app.worker

# MCP connector server
python -m app.mcp_server
```

---

## Next steps

Once the app is running, open **Settings** and configure:

1. **Search tab** -- job titles, location (`City, ST` format), radius
2. **Sources tab** -- API keys for at least one job board (Adzuna + Jooble recommended)
3. **Email tab** -- SMTP for digest notifications
4. **Candidate Profile tab** -- your master resume and documents
5. **AI tab** -- enable Automatic Features (add an AI provider) and/or the MCP Connector

See [Setup-Guide.md](../Setup-Guide.md) for step-by-step in-app configuration.
