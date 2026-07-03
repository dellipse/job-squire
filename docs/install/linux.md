# Installing on Linux

Two paths are covered here: **Docker / Podman** (recommended for production) and **native Python** (for local development or lightweight single-machine use).

> **Quickstart:** The install script handles runtime detection, configuration, and startup automatically:
> ```bash
> curl -fsSL https://raw.githubusercontent.com/dellipse/job-squire/main/install.sh -o install.sh
> bash install.sh
> ```
> If neither Docker nor Podman is installed, the script detects your distribution and offers to install one — showing you the exact commands before running anything. To undo a completed install, run `bash uninstall.sh` from the same directory.
>
> Follow the steps below if you prefer to set things up manually, or need more control over the configuration.

Not sure whether to use Docker or Podman? See [Docker vs. Podman](docker-vs-podman.md).

---

## Option A: Docker or Podman (recommended)

Docker Engine and Podman both work. The examples use `docker compose`; substitute `podman compose` or `podman-compose` if you are using Podman.

### 1. Install Docker Engine

Follow the official guide for your distribution:

- **Debian/Ubuntu:** https://docs.docker.com/engine/install/ubuntu/
- **Fedora/RHEL/CentOS:** https://docs.docker.com/engine/install/fedora/
- **Arch Linux:** `sudo pacman -S docker docker-compose` then `sudo systemctl enable --now docker`

Or install **Podman** instead:

```bash
# Debian/Ubuntu
sudo apt install podman podman-compose

# Fedora
sudo dnf install podman podman-compose
```

Add your user to the `docker` group to avoid typing `sudo` on every command:

```bash
sudo usermod -aG docker $USER
# Log out and back in, or: newgrp docker
```

### 2. Clone the repository

```bash
git clone https://github.com/dellipse/job-squire.git
cd job-squire
```

### 3. Create the data directory and environment file

```bash
mkdir -p data
cp examples/.env.example data/.env
chmod 660 data/.env
```

Open `data/.env` in an editor and set the required values:

```bash
# Generate a secure secret key:
python3 -c "import secrets; print(secrets.token_hex(32))"

# Paste the result into data/.env:
SECRET_KEY=<paste here>
ADMIN_PASSWORD=<your password>    # avoid $ characters
```

Find your host user and group IDs so the container process owns the data files:

```bash
id -u    # -> PUID
id -g    # -> PGID
```

Set `PUID` and `PGID` in `data/.env` to match these values.

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

### 5. (Optional) Add a reverse proxy for HTTPS

For production you will want TLS. The included compose file supports two modes:

- **Host-port mode** (default): the app binds to `127.0.0.1:8080` and `127.0.0.1:9000`. Point any reverse proxy (nginx, Caddy, Traefik, SWAG) at those ports.
- **Shared Docker network**: comment out the `ports` blocks in `docker-compose.yml` and uncomment the `networks` blocks. The proxy reaches the containers by name. Sample SWAG proxy confs are in `examples/nginx/`.

For a full production walkthrough including SWAG and Let's Encrypt, see [deployment.md](../deployment.md).

### Updating

```bash
cd job-squire
docker compose pull
docker compose up -d --no-deps --force-recreate job-squire job-squire-worker job-squire-mcp
```

---

## Option B: Native Python (no Docker)

Suitable for local development. The automated search worker and the MCP server can each be run as separate processes, but a production Linux server is better served by Option A.

> **Note:** DB cross-process locking uses `fcntl`, which is Linux-native and fully supported here.

### Prerequisites

- Python 3.12 (check with `python3 --version`)
- `git`

Install Python 3.12 if needed:

```bash
# Debian/Ubuntu
sudo apt install python3.12 python3.12-venv python3.12-dev

# Fedora
sudo dnf install python3.12

# Arch
sudo pacman -S python
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

For a quick local run, export them in your shell:

```bash
export SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
export ADMIN_PASSWORD=devpass
export USER_PASSWORD=devpass
export DATA_DIR=./data
export SESSION_COOKIE_SECURE=false
```

For a persistent configuration, create `data/.env` (using `examples/.env.example` as a template) and source it:

```bash
mkdir -p data
cp examples/.env.example data/.env
# Edit data/.env, then:
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
