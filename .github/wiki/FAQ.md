# FAQ

Common questions and real issues encountered during build and deployment. Check here first when something breaks.

---

## Containers and Deployment

**Q: The app starts but I see `$` truncated in my password / `WARN variable is not set`.**

Docker Compose interpolates `$` in `.env` file values. `$makem0n3y` becomes a truncated or empty variable. Avoid `$` in passwords, or escape it as `$$`.

---

**Q: The app shows `sqlite3.OperationalError: table jobs already exists` on first boot.**

The two gunicorn workers and the other containers raced on `create_all()` against a fresh database. This is fixed in the code via `fcntl.flock` on `/data/.init.lock`, which serializes DB initialization. If you see this again, check that the data directory is not mounted read-only and that the running user has write access.

---

**Q: The container can't write to `/data` -- permission denied.**

The container runs as `PUID`/`PGID` and `/data` is a host bind-mount. The host folder must be owned by that UID/GID:

```bash
chown -R <PUID>:<PGID> ./data
```

Run `id -u` and `id -g` on the host to confirm the right values.

---

**Q: I changed `PUID`/`PGID` and now nothing works.**

Changing `PUID`/`PGID` requires a rebuild (`--build`) and wiping the data volume so it re-initializes with the correct ownership. Back up your data first.

---

**Q: `curl https://mcp-squire.<domain>/health` returns an empty reply or a protocol error.**

nginx rejected the proxy-conf, so the request fell through to a default server. The cause is usually duplicate directives: `proxy_http_version`, `proxy_read_timeout`, and similar are already set by SWAG's bundled `proxy.conf`. If the MCP conf redeclares them, `nginx -t` fails and the reload silently does nothing.

Fix: the MCP proxy-conf must only contain `include proxy.conf`, a resolver, `proxy_pass`, and `http2 off`. Always run `docker exec swag nginx -t` after editing a conf.

---

**Q: The cert is stuck "Attempting to renew" / all renewals failed.**

Check `/containers/docker/swag/config/log/letsencrypt/letsencrypt.log`. If the failure is at ZeroSSL's ACME endpoint, switch SWAG to Let's Encrypt: set `CERTPROVIDER=letsencrypt` in the SWAG compose env and force-recreate the SWAG container.

---

**Q: How do I reset a user password?**

Set the new password in `data/.env`, add `RESET_UIDS_AND_PWDS_ON_START=true`, restart with `docker compose up -d`, confirm login, then remove the flag and restart again.

---

## Email

**Q: `SMTPAuthenticationError 535: Authentication failed`.**

For Brevo: the SMTP **Username** is the dedicated login shown on Brevo's SMTP & API page, not your Brevo account email. The **Password** is the SMTP key, not your Brevo account password.

For all providers: if the password field is left blank on save, the app retains the existing encrypted value. If you need to re-enter it, type the full password and save.

Use the **Send test email** button on the Settings > Email tab to verify credentials in isolation.

---

**Q: The search runs and finds jobs but no digest email arrives.**

Email only fires when a search run creates new jobs (not just finds them -- after the first run, subsequent runs will skip duplicates). Check:

1. SMTP is enabled on the Settings > Email tab.
2. The **History** tab shows the run status and whether "email" fired.
3. Use **Send test email** to confirm SMTP works independently of the search.

---

## Search and Providers

**Q: A provider shows an error in the run history.**

Usually a wrong or inactive API key. Re-paste the key on Settings > Sources. Use the per-provider **Test connection** button to validate the key in isolation. Use **Pull now** to run a search for just that provider (this also clears its cooldown).

---

**Q: A provider is being skipped ("in cooldown").**

After a 503 outage response, the provider is parked for `PROVIDER_COOLDOWN_HOURS` (default 4 hours) to avoid hammering a down API. It resumes automatically on the next scheduled run after the cooldown window. To force it now, click the provider's **Pull now** button.

---

**Q: The location field rejects my input ("Location must be City, ST").**

The job APIs and the scheduler need a parseable US city and state code: `Columbus, OH` or `San Jose, CA`. ZIP codes, full street addresses, and plain city names are rejected. Use the `radius` field to expand coverage instead.

---

**Q: The scheduler isn't running at the right time.**

The schedule fires in the **job-search location's** local time, derived automatically from the city/state in the Search settings (e.g. `Austin, TX` resolves to `America/Chicago`). The server's own clock or `TZ` environment variable is ignored for this purpose. To force a specific zone, set `SCHEDULE_TZ` to any IANA name (e.g. `America/New_York`). Changing schedule variables requires a restart of the worker container.

---

**Q: A run finds 0 new jobs.**

Normal after the first few runs. The "skipped" count shows how many duplicates were already in Job Squire. The four providers between them do catch most postings but no single feed is exhaustive.

---

**Q: "missing credential(s)... SECRET_KEY may have changed"**

A provider fails before making any API call because its required fields decrypt to blank strings. The usual cause is that `SECRET_KEY` changed after the keys were originally saved, so Fernet can no longer decrypt them. Fix: re-enter the provider API keys (and the SMTP password, and the Anthropic API key) on the Settings page.

---

## UI and Front End

**Q: A button or link does nothing when clicked.**

The app enforces a strict Content-Security-Policy that blocks all inline JavaScript. If a new handler was added as an inline `onclick` or `onsubmit`, it will be silently blocked by the browser. All client behavior must be wired in `app/static/app.js` via `data-*` attributes or CSS classes. Check the browser console for a CSP violation report.

---

**Q: "Open in Claude" / "Build kit in Claude" buttons are missing from the UI.**

These buttons are only rendered when AI mode is set to **MCP** (`AIConfig.mode == 'mcp'`). Confirm the mode is saved on Settings > AI tab. If you just deployed a code change, the container may be running the old image (rebuild) or the browser may have cached the old page (hard refresh with Ctrl+Shift+R).

---

**Q: A job URL is rejected ("Invalid URL").**

The job form uses WTForms `URL()` validation, which requires a valid TLD. `https://internal/path` is rejected; `https://company.com/jobs/123` is fine.

---

## AI Modes

**Q: Do I need an Anthropic API key to use API mode?**

No. API mode works with any configured AI provider. Add a free provider (Google Gemini, Groq, OpenRouter, Ollama, and others) under **Settings → AI → AI Providers**. The Anthropic API key is optional and can be used as a final fallback. Free tiers from Gemini and Groq are sufficient for typical job-search volumes.

---

**Q: How does provider fallback work?**

The Job Squire tries ranked providers in order. If a provider returns a rate-limit (429), server error (503/529), or times out, it moves immediately to the next one in the list. If all ranked providers fail and Anthropic fallback is enabled, it tries Anthropic last.

---

**Q: API "Analyze now" fails.**

If you have ranked providers configured, check the error message for which provider was last tried. Common causes: wrong or expired API key, free-tier quota exhausted, or invalid model name. If using Anthropic: verify the API key on Settings > AI tab (it is a separate pay-per-use key, not a Claude subscription) and confirm the model string (default `claude-sonnet-4-6`). The specific error class is shown in the flashed UI message.

---

**Q: The MCP connector won't connect in Claude.**

Work through these in order:

1. `curl https://mcp-squire.<domain>/health` must return `{"ok": true}`.
2. The URL you add in Claude is the **base URL** only -- no `/mcp/...` path, no token.
3. Auth is OAuth: when Claude opens the sign-in page, enter Job Squire **user** account credentials (not admin, not your Claude password).
4. Watch `docker logs -f job-squire-mcp` while connecting. You should see: discovery request, register, authorize, token exchange, then `ListToolsRequest` returning 200.
5. Confirm `PUBLIC_MCP_URL` and `PUBLIC_MCP_HOST` are set correctly in `data/.env`.

---

**Q: The MCP connector worked fine, then stopped after a container restart.**

OAuth tokens are now persisted to `DATA_DIR/oauth_tokens.json`, so a normal restart should not require re-authorization. If the token file is missing or corrupt, remove and re-add the connector in Claude (takes about 60 seconds) to go through the OAuth flow again.

---

## General Diagnostics

```bash
# Web app logs
docker logs job-squire

# Scheduler logs
docker logs job-squire-worker

# MCP logs (live, useful during connector setup)
docker logs -f job-squire-mcp

# Confirm a code change made it into the running image
docker exec job-squire grep -c "some_marker_string" /app/app/main.py

# Test nginx config after editing a proxy conf
docker exec swag nginx -t
```
