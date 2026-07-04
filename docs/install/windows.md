# Installing on Windows

Two paths are covered here: **Docker Desktop / Podman** (recommended) and **native Python**. The native Python path has limitations on Windows and is only suitable for local development.

---

## Option A: Docker Desktop or Podman (recommended)

Docker Desktop on Windows uses WSL 2 (Windows Subsystem for Linux) to run the containers. Most of the setup happens inside that Linux layer, so the experience is close to a native Linux install.

### 1. Install WSL 2 and Docker Desktop

**Enable WSL 2** (run in PowerShell as Administrator):

```powershell
wsl --install
# Restart when prompted. A Ubuntu distribution is installed by default.
```

**Install Docker Desktop:**

1. Download from https://www.docker.com/products/docker-desktop/
2. Run the installer. When asked, select **Use WSL 2 instead of Hyper-V**.
3. Launch Docker Desktop. Confirm the whale icon in the system tray shows "Docker Desktop is running."

**Or install Podman Desktop** (Docker-compatible, always free):

1. Download from https://podman-desktop.io/
2. Run the installer and follow the prompts. Podman Desktop manages a WSL-based Linux machine for you.

> Docker Desktop requires a license for commercial use in larger organizations.

### 2. Open a WSL terminal

All remaining commands run inside WSL, not in PowerShell or Command Prompt.

In Docker Desktop: click the terminal icon, or open the Ubuntu app from the Start menu.

```bash
# You are now in a Linux shell inside WSL.
```

### 3. Clone the repository

```bash
git clone https://github.com/dellipse/job-squire.git
cd job-squire
```

> **Storage tip:** Clone inside the WSL filesystem (e.g., `~/job-squire`), not under `/mnt/c/` (your Windows C: drive). File I/O from containers to `/mnt/c/` is significantly slower.

### 4. Create the data directory and environment file

```bash
mkdir -p data
cp examples/.env.example data/.env
chmod 660 data/.env
```

Open `data/.env` in an editor. You can use `nano data/.env` inside WSL, or open it in VS Code from WSL with `code data/.env`.

Set the required values:

```bash
# Generate a secure secret key:
python3 -c "import secrets; print(secrets.token_hex(32))"

# Paste the result into data/.env:
SECRET_KEY=<paste here>
ADMIN_PASSWORD=<your password>    # avoid $ characters
```

For local testing, also set:

```
SESSION_COOKIE_SECURE=false
```

> **PUID / PGID:** Run `id -u` and `id -g` inside WSL to get the right values and set them in `data/.env`. The defaults (`1000:1000`) usually match the first WSL user.

### 5. Start the containers

```bash
docker compose up -d
```

Verify startup:

```bash
docker compose logs job-squire          # gunicorn up, accounts seeded
docker compose logs job-squire-worker   # "scheduler up ..."
docker compose logs job-squire-mcp      # uvicorn on :9000
```

The web app is available in your Windows browser at **http://localhost:8080**. Sign in with `admin` and the password you set.

> Ports exposed by containers in WSL are automatically forwarded to Windows, so `localhost:8080` in a Windows browser reaches the container.

### 6. (Optional) HTTPS / reverse proxy

MCP mode requires a public HTTPS URL. For local HTTPS you can use Caddy:

```bash
# Inside WSL:
sudo apt install caddy
```

Create `~/Caddyfile`:

```
Job Squire.localhost {
    reverse_proxy localhost:8080
}
mcp-squire.localhost {
    reverse_proxy localhost:9000
}
```

```bash
caddy run --config ~/Caddyfile
```

Caddy issues a locally-trusted certificate automatically (you may need to accept it in Windows once).

For a full production deployment (Linux server + SWAG), see [deployment.md](../deployment.md).

### Updating

```bash
# Inside WSL:
cd job-squire
docker compose pull
docker compose up -d --no-deps --force-recreate job-squire job-squire-worker job-squire-mcp
```

---

## Option B: Native Python (no Docker)

Running the app natively on Windows is possible for local development, but has important limitations:

- The DB cross-process locking uses `fcntl`, which **does not exist on Windows**. The app will fail to start if the worker (`app.worker`) or MCP server (`app.mcp_server`) tries to acquire a cross-process lock alongside the web process. Run only one process at a time, or use the Docker path.
- Automated job search requires the worker process; without it you can trigger searches manually from the UI.
- The MCP server can be run as a standalone process if you are not running the web app simultaneously.

For a fully functional local setup on Windows, use WSL 2 with Docker (Option A) or the native Python path inside WSL (see [linux.md](linux.md)).

### Prerequisites

- **Python 3.12** from https://www.python.org/downloads/ — tick **Add python.exe to PATH** during install
- **Git for Windows** from https://git-scm.com/download/win

Open **PowerShell** or **Command Prompt** for the steps below.

### 1. Clone the repository

```powershell
git clone https://github.com/dellipse/job-squire.git
cd job-squire
```

### 2. Create a virtual environment and install dependencies

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

> On Windows, `python3` is typically just `python`. If `python` does not work, open the Microsoft Store and install Python 3.12.

### 3. Create the data directory and set environment variables

```powershell
mkdir data
```

Set environment variables in PowerShell for the session:

```powershell
$env:SECRET_KEY    = python -c "import secrets; print(secrets.token_hex(32))"
$env:ADMIN_PASSWORD = "devpass"
$env:USER_PASSWORD  = "devpass"
$env:DATA_DIR       = ".\data"
$env:SESSION_COOKIE_SECURE = "false"
```

Or create `data\.env` from the template and load it:

```powershell
copy examples\.env.example data\.env
# Edit data\.env in Notepad or VS Code, then in PowerShell:
Get-Content data\.env | Where-Object { $_ -notmatch '^\s*#' -and $_ -match '=' } | ForEach-Object {
    $parts = $_ -split '=', 2
    [System.Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), 'Process')
}
```

### 4. Start the web app

```powershell
python wsgi.py
# Web app at http://localhost:8000
```

### 5. (Optional) Run the MCP server in a separate terminal

```powershell
# New PowerShell window, with the virtual environment activated and env vars set:
python -m app.mcp_server
```

> Do not run `app.worker` simultaneously with `wsgi.py` on Windows. The `fcntl` lock will fail. If you need automated job searches, use the Docker setup.

---

## Next steps

Once the app is running, open **Settings** and configure:

1. **Search tab** -- job titles, location (`City, ST` format), radius
2. **Sources tab** -- API keys for at least one job board (Adzuna + Jooble recommended)
3. **Email tab** -- SMTP for digest notifications
4. **Candidate Profile tab** -- your master resume and documents
5. **AI tab** -- enable Automatic Features (add an AI provider) and/or the MCP Connector

See [Setup-Guide.md](../Setup-Guide.md) for step-by-step in-app configuration.
