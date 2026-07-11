# Running Multiple Instances

> **Heads up:** this doc predates the instance-registry and cookie-collision handling designed
> into the eventual `job-squire` CLI. It's still accurate for manually running multiple
> `docker compose -p <name>` instances as described here — see
> [`PLAN-deployment-modes.md`](PLAN-deployment-modes.md) Section 4 for where per-instance
> identity (name, cookie name, ports) is headed.

You can run more than one independent JobSquire deployment on the same physical host — for example, one instance per job seeker. Each instance is fully isolated: its own database, uploads, credentials, URLs, and OAuth tokens. They share only the Docker image.

---

## What must differ between instances

| What | Where set | Why |
|---|---|---|
| `SECRET_KEY` | `data/.env` | Signs sessions and derives the encryption key for stored secrets. Sharing it across instances would let one instance decrypt the other's secrets. |
| `ADMIN_PASSWORD` / `USER_PASSWORD` | `data/.env` | Independent accounts per instance. |
| `DATA_HOST_DIR` | `data/.env` | The host path bind-mounted to `/data`. Each instance must point to a different directory on disk. |
| `PUBLIC_URL` | `data/.env` | The instance's web URL, used in notification emails. |
| `PUBLIC_MCP_URL` / `PUBLIC_MCP_HOST` | `data/.env` | The instance's MCP URL. Each instance needs its own subdomain. |
| `APP_HOST_PORT` | root `.env` or shell | The host port for the web container. Must not clash with another instance. |
| `MCP_HOST_PORT` | root `.env` or shell | The host port for the MCP container. Must not clash with another instance. |
| Docker compose project name | `-p` flag | Namespaces container names. Without this, the second instance would conflict with the first. |

> **`APP_HOST_PORT` and `MCP_HOST_PORT` are read by docker-compose at launch time** — they must be in the shell environment or a `.env` file next to `docker-compose.yml` (see [Configuration: host port variables](configuration.md)). They are not read from `data/.env`.

---

## Directory layout for two instances

```
job-squire/                  ← shared source / docker-compose.yml / image
  docker-compose.yml
  examples/
  app/
  ...

data-alice/                   ← instance 1 data + env
  .env
  job-squire.db
  uploads/
  candidate_profile.md

data-bob/                     ← instance 2 data + env
  .env
  job-squire.db
  uploads/
  candidate_profile.md
```

The `data-*/` folders are the bind mounts. They live alongside the source tree (or anywhere on the host — set `DATA_HOST_DIR` accordingly).

---

## Step-by-step: adding a second instance

### 1. Create the data directory and env file

```bash
mkdir -p data-bob
cp examples/.env.example data-bob/.env
chmod 660 data-bob/.env
```

Edit `data-bob/.env`. At minimum, set these to values that differ from the first instance:

```bash
SECRET_KEY=<new unique key>
ADMIN_PASSWORD=<password>
USER_PASSWORD=<password>                         # optional
DATA_HOST_DIR=./data-bob                         # or an absolute path
PUBLIC_URL=https://Job Squire-bob.yourdomain.com
PUBLIC_MCP_URL=https://mcp-bob.yourdomain.com
PUBLIC_MCP_HOST=mcp-bob.yourdomain.com
MCP_PORT=9000                                    # container-internal; can stay the same
```

### 2. Set the host port overrides

Create a `.env` file in the same directory as `docker-compose.yml` (the compose root), or export the variables in your shell before running `docker compose`. These values are **not** in `data-bob/.env` — they are consumed by docker-compose itself, not by the containers.

```bash
# compose-root/.env  (next to docker-compose.yml)
APP_HOST_PORT=8081      # must differ from instance 1's 8080
MCP_HOST_PORT=9001      # must differ from instance 1's 9000
```

Alternatively, export them inline:

```bash
APP_HOST_PORT=8081 MCP_HOST_PORT=9001 docker compose -p bob \
  --env-file data-bob/.env up -d
```

### 3. Add SWAG proxy configs

Copy the sample nginx configs and rename them for this instance:

```bash
cp examples/nginx/job-squire.subdomain.conf \
   /path/to/swag/config/nginx/proxy-confs/Job Squire-bob.subdomain.conf

cp examples/nginx/mcp-squire.subdomain.conf \
   /path/to/swag/config/nginx/proxy-confs/mcp-bob.subdomain.conf
```

Edit `Job Squire-bob.subdomain.conf` — change `server_name` and `proxy_pass`:

```nginx
server_name Job Squire-bob.*;

# Option A (host-port mode): proxy to the instance's host port
proxy_pass http://127.0.0.1:8081;

# Option B (shared network): proxy by container name
# set $upstream_app job-squire-bob;      ← the project-namespaced container name
# set $upstream_port 8000;
```

Edit `mcp-bob.subdomain.conf` similarly:

```nginx
server_name mcp-bob.*;
proxy_pass http://127.0.0.1:9001;
# Option B: set $upstream_app job-squire-mcp-bob;
```

> In Option B (shared Docker network), docker-compose adds the project name as a prefix to container names. With `-p bob`, the containers become `bob-job-squire-1`, `bob-job-squire-mcp-1`, etc. Check the actual names with `docker compose -p bob ps` and use them in the proxy conf.

Reload SWAG:

```bash
docker exec swag nginx -t && docker exec swag nginx -s reload
```

### 4. Start the second instance

```bash
cd job-squire

APP_HOST_PORT=8081 MCP_HOST_PORT=9001 \
docker compose -p bob --env-file data-bob/.env up -d
```

The `-p bob` flag gives this set of containers its own namespace. Without it, `docker compose up` would either reuse or conflict with the first instance's containers.

Verify:

```bash
docker compose -p bob ps              # all three containers running
docker compose -p bob logs job-squire     # gunicorn up, accounts seeded
docker compose -p bob logs job-squire-mcp # uvicorn on :9000

curl -s http://127.0.0.1:8081/health   # {"ok": true}
curl -s https://mcp-bob.yourdomain.com/health   # {"ok": true}
```

---

## MCP connector per instance

Each instance's MCP server is a separate OAuth authorization server with its own token store. You connect each one independently in Claude.

For each instance:

1. Ensure `PUBLIC_MCP_URL` and `PUBLIC_MCP_HOST` are set correctly in that instance's `data/.env`.
2. In Claude: **Settings > Connectors > Add custom connector**. Paste this instance's `PUBLIC_MCP_URL`.
3. Claude opens the OAuth sign-in page for that instance. Enter the job seeker's credentials for that instance.
4. Repeat for each additional instance — Claude stores a separate token per connector URL.

---

## Updating instances

Pull the latest image once; it is shared:

```bash
cd job-squire
docker compose pull   # updates ghcr.io/dellipse/job-squire:latest
```

Then restart each instance separately:

```bash
APP_HOST_PORT=8080 MCP_HOST_PORT=9000 \
docker compose -p alice --env-file data-alice/.env \
  up -d --no-deps --force-recreate job-squire job-squire-worker job-squire-mcp

APP_HOST_PORT=8081 MCP_HOST_PORT=9001 \
docker compose -p bob --env-file data-bob/.env \
  up -d --no-deps --force-recreate job-squire job-squire-worker job-squire-mcp
```

---

## Backups

Back up each instance's data folder independently:

```bash
tar czf alice-backup-$(date +%F).tgz -C data-alice .
tar czf bob-backup-$(date +%F).tgz   -C data-bob   .
```

---

## Quick reference — per-instance variables

| Variable | Where | Per-instance? | Notes |
|---|---|---|---|
| `SECRET_KEY` | `data/.env` | Required | Generate a new one for each instance |
| `ADMIN_PASSWORD` / `USER_PASSWORD` | `data/.env` | Required | Separate credentials |
| `DATA_HOST_DIR` | `data/.env` | Required | Different host path for each |
| `PUBLIC_URL` | `data/.env` | Required | Different subdomain or domain |
| `PUBLIC_MCP_URL` / `PUBLIC_MCP_HOST` | `data/.env` | Required | Different MCP subdomain |
| `MCP_PORT` | `data/.env` | Optional | Container-internal port; same value is fine since containers are isolated |
| `APP_HOST_PORT` | root `.env` / shell | Required | Different host port per instance (default 8080) |
| `MCP_HOST_PORT` | root `.env` / shell | Required | Different host port per instance (default 9000) |
| `-p <name>` | `docker compose` flag | Required | Unique project name per instance |
