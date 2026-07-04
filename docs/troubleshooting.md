# Troubleshooting

Real issues hit during build/deploy, with cause and fix. Check here first.

## Deployment / containers

### `$` in a password didn't work / `WARN ... variable is not set`
Docker Compose interpolates `$` in `.env` values, so `$makem0n3y` became an empty/truncated
value. **Fix:** avoid `$` in passwords, or escape it as `$$` in `.env`.

### `sqlite3.OperationalError: table jobs already exists` on first boot
The 2 gunicorn workers (and the other containers) raced on `create_all()` against a fresh DB.
**Fix (already in code):** `_init_database()` serializes setup with an exclusive `fcntl.flock` on
`/data/.init.lock`. If you ever see this again, the lock file or volume permissions are the place
to look.

> **Note:** `fcntl.flock` is Linux-only. The app is designed to run in Docker on Linux hosts.
> If you run it directly on macOS or Windows for local development (not in Docker), the lock
> falls back gracefully, but concurrent startup races are theoretically possible on a fresh DB.

### App can't write to `/data` / permission denied
The container runs as the UID/GID set via `PUID`/`PGID` build args and `/data` is a **host bind
mount** (`DATA_HOST_DIR`), so the host folder must be owned by that UID/GID. If you change
`PUID/PGID` or the folder is owned by someone else, the container user can't write. **Fix:**
`sudo chown -R <PUID>:<PGID> ./job-squire/data`.

### `curl: (52) Empty reply` / `(92) PROTOCOL_ERROR` on the MCP subdomain
nginx rejected the proxy-conf, so the subdomain fell through to a default server. The cause was
**duplicate directives**: `proxy_http_version` and `proxy_read_timeout` are already set by SWAG's
bundled `proxy.conf`, and redeclaring them makes `nginx -t` fail (`directive is duplicate`).
**Fix:** the MCP proxy-conf must NOT set any `proxy_*` directive itself — just `include
proxy.conf` + resolver + `proxy_pass`. Also `http2 off` for the SSE endpoint. Always run
`sudo docker exec swag nginx -t` after editing a conf; the reload only happens if it passes.

### SWAG wasn't running / `No such container: swag`
The stack was started with only Job Squire service names, so SWAG was never brought up.
**Fix:** Start SWAG from your orchestrator compose, or run `docker compose up -d` with no
service name to start everything.

### Certificate stuck "Attempting to renew" / `All renewals failed`
The certbot debug log (`/containers/docker/swag/config/log/letsencrypt/letsencrypt.log`) showed
the failure at `finalize_order` talking to **ZeroSSL's** ACME API (`RemoteDisconnected`). Cloudflare
DNS validation was fine. **Fix:** switch SWAG to Let's Encrypt — set `CERTPROVIDER=letsencrypt`
(same Cloudflare DNS plugin) and `up -d --force-recreate swag`.

## Email

### `SMTPAuthenticationError: (535, '5.7.8 Authentication failed')`
Credential rejection. For **Brevo**: the **Username** is the dedicated SMTP login shown on Brevo's
SMTP & API page — **not** the Brevo account email. The **Password** is the SMTP key — not the
account password. Also note the app keeps the saved password if the field is left blank, so
re-enter the key and Save before testing. Use **Send test email** to verify.

### No digest emails even though search finds jobs
Email only fires when a run creates **new** jobs and SMTP is enabled. Check the SMTP "enabled"
toggle, and the `SearchRun` row's "email" column on the Settings page (History tab). The `Send test email`
button isolates SMTP from search.

## UI / front end

### A button or interaction does nothing in the browser
The CSP is `script-src 'self'`, which blocks **all inline JavaScript** (inline `onclick`,
`onchange`, `onsubmit`, inline `<script>`). This once broke clickable rows, dropdowns, confirms,
and the kit button. **Fix (already in code):** all client JS is in `app/static/app.js` and wired
via `data-*` attributes / classes. **Never** add inline handlers — add behavior to `app.js`.

### "Build kit in Claude" / "Open in Claude" buttons missing
They show when `claude_buttons_enabled` is set — which happens automatically when the MCP Connector
is enabled and a connector name is saved in Settings → AI → MCP Connector. Confirm both are set and
saved. If you just changed the code, the container may be running the old image (rebuild) or the
browser cached the old page/JS (hard refresh).

### Form rejects a URL ("Invalid URL")
`JobForm`/`KitForm` use WTForms `URL()`, which requires a TLD. `https://x/job` is rejected;
`https://example.com/job` is fine. Real postings have valid URLs.

## Search / providers

### A provider shows an error in the run history
Usually a wrong/inactive API key — re-paste it on the Settings page (Sources tab). `search_provider()` never
raises, so one bad provider can't kill a run; the error is recorded in `SearchRun.detail`. Use the
per-provider **Test connection** button to verify one key in isolation, and **Pull now** to run a
full search for just that provider (this also clears its cooldown).

### "missing credential(s)... SECRET_KEY may have changed"
A provider fails before any API call because its required fields decrypt to blank. The usual cause
is that `SECRET_KEY` changed after the keys were saved, so Fernet can no longer decrypt them.
**Fix:** re-enter the provider keys (and SMTP password, Anthropic key) on the Settings page.

### A provider is being skipped ("in cooldown")
After a 503 outage, that provider is parked for `PROVIDER_COOLDOWN_HOURS` (default 4) and the run
history says so. It resumes automatically; to force it now, use the provider's **Pull now** button,
which clears the cooldown.

### "Location must be City, ST..." when saving search settings
The job APIs and the scheduler need a parseable city/state, so the Search tab rejects ZIP codes and
street addresses. Enter `City, ST` with a valid US state code (e.g. `Spokane, WA`) and widen the
**radius** instead of using a ZIP.

### A run finds 0 new jobs
Normal once the providers have already pulled in everything currently posted. The "skipped" count
shows duplicates that were deduped.

## AI modes

### Do I need an Anthropic API key?
No. API mode works with any configured AI provider — add a free one (Google Gemini, Groq,
OpenRouter, Ollama, and others) under Settings → AI → AI Providers. An Anthropic key is optional
and can be added as a final fallback. Free tiers from Gemini and Groq are sufficient for typical
job-search volumes. See [Setting Up AI](wiki/10-ai-setup.md) for provider strategies.

### "Analyze now" fails
The Job Squire tries your configured AI providers in rank order: if a provider returns a rate-limit
(429), server error (503/529), or times out, it moves immediately to the next one in the ranked
chain. Check the provider cards under Settings → AI → AI Providers: verify the API key is correct,
the provider is enabled, and the model string is valid — the error message names which provider was
last tried. If you are using Anthropic directly (no ranked providers), check the Anthropic API key
on the AI tab and the model string (default `claude-sonnet-4-6`). The specific error class is shown
in the flashed UI message.

### MCP connector won't connect in Claude
`curl https://mcp-squire.<domain>/health` must return `{"ok": true}` first. The connector URL is
the **base** URL (`https://mcp-squire.<domain>`) — no `/mcp/...` path. Auth is OAuth: Claude opens
a sign-in page where you enter your **Job Squire** username/password (not your Claude password). Watch
`docker logs -f job-squire-mcp` while connecting — you should see discovery → register → authorize
→ token, then `ListToolsRequest` returning 200. Confirm `PUBLIC_MCP_URL` (and `PUBLIC_MCP_HOST` if
the public host differs) are set in `data/.env`.

### MCP connector worked, then stopped after a restart
OAuth tokens are persisted to `DATA_DIR/oauth_tokens.json`, so a normal container
restart should not require re-authorization. If you still lose the connection, the
token file may be missing or corrupt.
**Fix:** in Claude, remove and re-add the connector (~10 seconds) to re-authorize.
The token file is regenerated automatically.

## General diagnostics

- Web app logs: `sudo docker logs job-squire`
- Scheduler logs: `sudo docker logs job-squire-worker`
- MCP logs: `sudo docker logs -f job-squire-mcp`
- Confirm the running image has a given change:
  `sudo docker exec job-squire grep -c <marker> /app/app/<file>`
- nginx config test (after any proxy-conf edit): `sudo docker exec swag nginx -t`
