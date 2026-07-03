# Setup Guide

Step-by-step onboarding for a new deployment. The target environment is a Linux host running Docker with SWAG as the reverse proxy. Adjust paths to match your host layout.

---

## Prerequisites

- Docker Engine and Docker Compose v2 (`docker compose`, not `docker-compose`)
- A running SWAG instance (or any nginx-based proxy) for TLS termination
- A domain with DNS pointing to your host (needed for Let's Encrypt certs and the MCP connector)
- Optional: free API keys for job sources (get at least one before running your first search)

---

## Step 1: Get the Code

```bash
git clone https://github.com/dellipse/job-squire.git
cd job-squire
```

Or pull the pre-built image directly (no source needed for a production deploy):

```bash
# The compose file references ghcr.io/dellipse/job-squire:latest
# It will pull automatically on first `docker compose up`
```

---

## Step 2: Create the Data Directory and Env File

```bash
mkdir -p data
cp examples/.env.example data/.env
chmod 660 data/.env
```

Open `data/.env` in an editor. The minimum required values:

```bash
# Generate a random secret key:
python3 -c "import secrets; print(secrets.token_hex(32))"

SECRET_KEY=<paste generated value>
ADMIN_PASSWORD=<your admin password>   # avoid $ characters
```

Optional second account (the job seeker):

```bash
USER_PASSWORD=<job seeker password>
```

Set your public URLs if you are deploying behind SWAG:

```bash
SESSION_COOKIE_SECURE=true
PUBLIC_URL=https://squire.yourdomain.com
PUBLIC_MCP_URL=https://mcp-squire.yourdomain.com
PUBLIC_MCP_HOST=mcp-squire.yourdomain.com
```

Find your host user/group IDs for file permissions:

```bash
id -u   # PUID
id -g   # PGID
```

Set `PUID` and `PGID` in `data/.env` to match. The data folder must be owned by this UID/GID.

---

## Step 3: Configure the Reverse Proxy

### Option A: Host-port mode (simplest)

The default compose file publishes the web app on `127.0.0.1:8080` and the MCP server on `127.0.0.1:9000`. Point your proxy at these addresses. No changes to `docker-compose.yml` are needed.

### Option B: Shared Docker network with SWAG

Create the shared network once:

```bash
docker network create swag
```

In `docker-compose.yml`, for both `job-squire` and `job-squire-mcp`: comment out the `ports` blocks and uncomment the `networks` blocks and the bottom-level `networks:` declaration.

Copy the sample proxy confs:

```bash
cp examples/nginx/job-squire.subdomain.conf \
   /path/to/swag/config/nginx/proxy-confs/

cp examples/nginx/mcp-squire.subdomain.conf \
   /path/to/swag/config/nginx/proxy-confs/
```

Edit each file to replace `yourdomain.com` with your actual domain.

> The MCP proxy conf uses `http2 off`. Do not add any `proxy_*` directives -- SWAG's bundled `proxy.conf` already sets them, and duplicates will fail `nginx -t`.

Test and reload SWAG:

```bash
docker exec swag nginx -t
docker exec swag nginx -s reload
```

---

## Step 4: Start the Containers

```bash
docker compose up -d
```

Verify startup:

```bash
docker compose logs job-squire          # gunicorn up, accounts seeded, no traceback
docker compose logs job-squire-worker   # "scheduler up ..."
docker compose logs job-squire-mcp      # uvicorn on :9000
```

Test connectivity:

```bash
curl -is https://squire.yourdomain.com/ | head -1
# HTTP/2 200

curl -s https://mcp-squire.yourdomain.com/health
# {"ok": true}
```

---

## Step 5: In-App Setup

Sign in at `https://squire.yourdomain.com` with `admin` and the password from `data/.env`.

Open **Settings** and work through each tab:

**Search tab**

- Enter job titles (one per line).
- Set location as `City, ST` (e.g. `Austin, TX`). ZIP codes and street addresses are rejected.
- Set radius, minimum salary (optional), and max posting age.

**Sources tab**

For each job board you want to use:

1. Click the "get a key" link and sign up (all are free).
2. Paste the key(s) into the fields.
3. Tick "Use this source" and save.

Adzuna + Jooble is the recommended starting pair. They provide good coverage for most US metro markets.

**Email tab**

Fill in your SMTP settings and enable notifications. Click **Send test email** to verify.

> Brevo users: the **Username** is the dedicated SMTP login on Brevo's SMTP & API page, not your Brevo account email. The **Password** is the SMTP key, not your account password.

**Candidate Profile tab**

Edit the master profile (or upload a `.md` file). This is the source of truth for every application kit and the MCP `get_candidate_profile` tool. It lives on disk at `DATA_DIR/candidate_profile.md` and survives image rebuilds.

Use the **Document library** to upload the base resume, recommendation letters, certs, and portfolio items.

**AI tab** (optional)

Choose a mode:

- **Manual** -- no setup needed. You copy/paste JSON to and from Claude.
- **API** -- paste an Anthropic API key. Enables one-click analysis and automated routines.
- **MCP** -- requires the MCP container and a public HTTPS URL (see Step 6).

**Application Kit tab**

Set the salary floor for fit assessments (default `$60,000`). Postings below this are flagged in the kit.

---

## Step 6: Connect the MCP Connector (MCP Mode Only)

Prerequisites: `PUBLIC_MCP_URL` is set in `data/.env` and the `job-squire-mcp` container is running.

1. On **Settings > AI tab**, set mode to **MCP** and save.
2. In Claude: go to **Settings > Connectors > Add custom connector**.
3. Paste the base URL shown on the Settings page (e.g. `https://mcp-squire.yourdomain.com`).
4. Claude opens an OAuth sign-in page. Enter Job Squire `user` account credentials (not the admin account).
5. Claude completes the handshake. The connector shows as active.

Verify by watching the MCP logs while connecting:

```bash
docker logs -f job-squire-mcp
# You should see: discovery -> register -> authorize -> token, then ListToolsRequest 200
```

**Open in Claude** buttons now appear on the AI tab, on individual job pages, and on the Jobs list.

---

## Step 7: Run Your First Search

1. Go to **Settings > Search tab**.
2. Click **Run search now**.
3. New jobs appear under the `Saved` status on the Jobs page.
4. A digest email goes to the configured address if anything was found.

Check the **History tab** to see the run log.

---

## Updating to a New Version

```bash
cd job-squire
docker compose pull
docker compose up -d --no-deps --force-recreate job-squire job-squire-worker job-squire-mcp
```

Hard-refresh the browser after updating (Ctrl+Shift+R / Cmd+Shift+R) to clear cached CSS/JS.

If `requirements.txt` changed, add `--build` to the `up` command.

---

## Resetting a Password

1. Set the new password in `data/.env`.
2. Add `RESET_UIDS_AND_PWDS_ON_START=true` to `data/.env`.
3. `docker compose up -d`
4. Confirm you can log in.
5. Remove the `RESET_UIDS_AND_PWDS_ON_START` line and `docker compose up -d` again.

---

## Local Dev (No Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export SECRET_KEY=dev \
       ADMIN_PASSWORD=devpass \
       USER_PASSWORD=devpass \
       DATA_DIR=./data \
       SESSION_COOKIE_SECURE=false

mkdir -p data
python wsgi.py
# http://localhost:8000
```

`fcntl`-based DB locking is Linux-only. On macOS, the lock degrades gracefully for single-process dev use.

---

## Backups

The entire Job Squire lives in one directory:

```bash
tar czf job-squire-backup-$(date +%F).tgz -C ./data .
```

Contents: `job-squire.db`, `uploads/`, `candidate_profile.md`, `oauth_tokens.json`.

---

## Wiping Data (Dev / Re-Init)

Stops Job Squire services, clears the DB and uploads, and brings them back up with a fresh database:

```bash
docker compose rm -sf job-squire job-squire-worker job-squire-mcp
rm -rf ./data/{job-squire.db,job-squire.db-*,uploads,.init.lock,provider_cooldowns.json}
docker compose up -d job-squire job-squire-worker job-squire-mcp
```

Omit `candidate_profile.md` from the rm to preserve the master profile. The bundled default is re-seeded from the image on first boot if the file is missing.
