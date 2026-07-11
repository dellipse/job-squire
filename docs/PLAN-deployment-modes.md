# Deployment Modes and Single-Container Design

**Status:** Draft for review (2026-07-11). Nothing here is implemented yet. This document is being reviewed section by section before any code changes. The Section 8 open questions were resolved on 2026-07-11; the two `jobsquire-cli` items remain deferred to the dedicated CLI session.

**Supersedes on completion:** the current `deployment.md`, `multi-instance.md`, and `backup-restore.md`, plus parts of `configuration.md` and `architecture.md`, with the user setup guide rewritten. Those stay authoritative until this design ships. The full disposition is in Section 8.

## Contents

1. Overview, goals, and the three deployment modes
2. Single-container architecture on s6-overlay
3. Configuration model: the `DEPLOY_MODE` preset, granular overrides, and the env matrix
4. Instance model and the cross-platform registry
5. Networking and security per mode, including MCP authentication
6. Container runtime selection and install
7. Lifecycle and `job-squire` CLI touchpoints
8. Migration path and open questions

---

## 1. Overview, goals, and the three deployment modes

### Purpose

Job Squire needs to run well in three different situations without becoming three different products. A retiree running it on a single laptop, two unrelated people sharing one machine but keeping their searches completely separate, and a small classroom or lab running a handful of installs behind a firewall are all the same application. This document defines one codebase, one container image, and one setup flow that adapts to each situation through configuration and a per-install identity, rather than through forks or build-time variants.

### Goals

The design aims to keep a single source of truth. One image, built once, runs everywhere. Behavior differs only by environment configuration chosen at setup, never by conditional builds or separate branches. Setup must be approachable for a non-technical person whose real goal is finding a job, so the details described here are things the tooling handles on their behalf and that they never need to see unless they want to. The design must work on macOS, Windows, and Linux, on both Intel and ARM processors, and must treat security and privacy as defaults rather than options. There is a single entry point: one command that downloads and installs the `job-squire` CLI from the official GitHub repository (the latest version by default, with an option to pin a specific version) and then launches it, if the platform allows, to drive installation and setup. Everything else is part of that CLI. Nothing is a separate install script. The CLI is the single front door for starting, stopping, updating, configuring, and creating instances. The mechanics of the bootstrap command are specified in Section 6 and the lifecycle operations in Section 7; the CLI's full capability design is handled in its own dedicated session.

### Non-goals

Job Squire is not becoming a multi-tenant application. A single running instance still serves exactly two accounts, an admin and a user, exactly as it does today. When this document talks about supporting multiple people on one machine, it means running the program more than once as fully separate instances, each with its own database and its own data on disk, not one program partitioning users internally. Anyone who wants to share credentials or data between instances does that outside the application. Keeping the app single-tenant per instance is a deliberate choice that avoids authentication complexity and limits the blast radius if any one instance is compromised.

### The three deployment modes

Every install is one of three modes. The first two run on plain HTTP over the loopback interface, which modern browsers treat as a secure context, so there are no certificate warnings and no reverse proxy is required. The third is the only mode exposed to a network, and it always sits behind an external reverse proxy that terminates TLS.

| Mode | Who it is for | Exposure | TLS / proxy | Instance isolation |
|---|---|---|---|---|
| Local, single | One person on their own machine | `http://localhost:PORT` (loopback only) | None; loopback is a trusted secure context | One instance, its own data directory |
| Local, multi-instance | Two or more people sharing one machine, kept fully separate | `http://localhost:PORT` per instance, distinct ports | None; loopback | Separate ports and unique session cookie names |
| Network | A server, small lab, or classroom (roughly five instances at most) | A hostname per instance | Mandatory external reverse proxy terminates TLS; app never does | Distinct hostnames routed to distinct instances |

The modes are not separate code paths. They are presets over a small set of configuration flags, described in Section 3, combined with the per-install instance identity described in Section 4. A "local single" install is simply the common case of the same machinery that supports the other two, which is why even a single install answers the same setup questions and receives a unique instance name.

### The unifying idea

Two ideas carry the whole design. First, a deployment mode is only a convenience preset that fills in sane defaults for a handful of granular settings, and the running code always reads the granular settings, never the mode label. Second, every install has an instance identity, a unique name that deterministically drives its data location, its database, its encryption key, its session cookie name, its container project name, and its port or hostname. Together these let one image and one setup flow cover local single, local multi-instance, and network installs on every supported operating system and processor architecture.

---

## 2. Single-container architecture on s6-overlay

### From three containers to one

Today Job Squire ships as one image run three times: the web app under gunicorn, the scheduler under `python -m app.worker`, and the MCP server under `python -m app.mcp_server`. All three share one SQLite database on a bind-mounted `/data` directory, and each has its own healthcheck. This design keeps the exact same three processes and the same shared database, but runs them inside a single container instead of three. The motivation is footprint and simplicity: an instance becomes one container to start, stop, update, and reason about, which matters directly when several instances run on one machine and when a non-technical person is meant to never think about any of this.

### One container is not one process

Collapsing to one container does not mean collapsing to one process, and this is the crucial constraint. The scheduler must fire each slot exactly once. That is precisely why the worker runs as its own process today rather than inside gunicorn, where it would run once per gunicorn worker and duplicate every search. The MCP server also listens on its own port and has its own lifecycle. So inside the single container we still run three distinct long-running processes, coordinated by a real process supervisor. The supervisor's job is not cosmetic: it must be PID 1, forward termination signals so the database shuts down cleanly, restart an individual process if it dies, and order startup so the web and database initialization come up before the worker and MCP attach to the same database.

### s6-overlay as PID 1

The supervisor is s6-overlay, running as PID 1 on the LinuxServer `baseimage-alpine` base image, a decision settled in an earlier session. The three processes become three s6 longrun services defined under `/etc/s6-overlay/s6-rc.d/`.

| s6 service | Runs | Listens | Notes |
|---|---|---|---|
| `web` | gunicorn, 2 workers, `wsgi:app` | `8000` inside the container | The primary service; performs first-boot DB init, migrations, and seeding. |
| `worker` | `python -m app.worker` (APScheduler) | none | Single process, so each scheduled slot fires exactly once. Starts after `web`. |
| `mcp` | `python -m app.mcp_server` (uvicorn, Streamable HTTP) | `9000` inside the container | Claude's connector endpoint. Starts after `web`. |

Startup ordering is expressed as s6 service dependencies so the worker and MCP wait for the web service, which owns database initialization, migrations, and seeding, to become ready. This preserves the current `depends_on: service_healthy` behavior that compose gives us across three containers, now expressed inside one.

### Signal handling and safe shutdown

Because the database is SQLite in WAL mode, a clean shutdown matters. s6-overlay as PID 1 forwards `SIGTERM` to each service on `docker stop`, so gunicorn, the worker, and uvicorn each get the chance to finish in-flight work and close the database cleanly rather than being killed mid-write. This is the specific reason a naive approach of backgrounding three processes in a shell script is not acceptable: such a script does not propagate signals or reap children correctly, and it would put WAL integrity at risk on every stop and every update. s6 handles signal forwarding, child reaping, and per-service restart as first-class behavior.

### Health and observability

With three containers, each has its own healthcheck. In one container we aggregate them into a single container-level healthcheck that passes only when all three internal probes pass: the web `/health` endpoint on `8000`, the MCP `/health` endpoint on `9000`, and the worker liveness check, which stays the existing heartbeat file (`.worker_heartbeat`) that the worker touches on an interval. The heartbeat remains valuable precisely because the worker has no HTTP endpoint, and it detects a wedged scheduler independently of whether search is enabled. The in-app worker-status banner carries over unchanged, and the WAL-safe backup approach still applies since the process topology inside the container is the same as before; the backup mechanics themselves are redesigned into the CLI's single encrypted archive (Section 7).

### Isolation trade-off, stated honestly

Consolidation costs one thing worth naming: with three containers you can restart or inspect a single component in isolation, and a crash is contained to that container. In one container, s6 will restart an individual failed service, which covers the common case, but the three services now share a container lifecycle and a kernel namespace. For a two-user-per-instance application running at most a handful of instances on a machine, this is an acceptable trade for the large gains in simplicity and footprint. Anyone who genuinely needs component-level isolation can still run the legacy three-container compose, which remains in the repository during migration.

### Multi-architecture build

The single most important build change is that the image must be multi-architecture. Host operating system is not the real axis, because containers always run on Linux; on macOS and Windows they run inside a Linux virtual machine. The real axis is CPU architecture, so the image is built for both `linux/amd64` and `linux/arm64` using `docker buildx --platform linux/amd64,linux/arm64`, which requires adding the QEMU and buildx setup steps to CI, where the pipeline currently builds a single architecture to GHCR. With a correct multi-arch image, Intel and Apple-silicon Macs, Intel and ARM Linux hosts, and Windows on either architecture all pull the right variant automatically and no per-architecture handling leaks into setup.

### Base image and the musl caveat

Two base-image details must be respected. First, the LinuxServer Alpine base uses musl rather than glibc, so the full requirements set must install and run on musl, meaning musllinux wheels exist for anything with native code. This has now been verified against the actual lockfile: resolving the complete dependency tree for musl at the base's Python version yields binary wheels for every package with no source builds, including `cryptography` (which backs the Fernet secret encryption and ships a `musllinux_1_2` abi3 wheel), `lxml`, `pydantic-core`, `greenlet`, `sqlalchemy`, and the rest. The `pydantic` and `pydantic-core` versions should be pinned in the lockfile so this resolution stays deterministic. If a future dependency ever resists musl, the documented fallback remains a Debian-slim base with `tini` as PID 1 and s6 or supervisord, or the larger LinuxServer Ubuntu base. Second, LinuxServer deprecated its `baseimage-alpine-python`, so Python is installed on top of `baseimage-alpine` with `apk add python3 py3-pip`. This provides the Python version carried by the pinned Alpine release, which for the current base (Alpine 3.23) is Python 3.12, not 3.14; the application runs on 3.12 without change, and 3.12 has the broadest musllinux wheel coverage, so the base's stock Python is used rather than forcing a newer one. The base image is pinned to a dated tag because these images have no `latest` and can make breaking changes between versions.

### Init branding

The LinuxServer base prints a branding banner during container init, and LinuxServer asks that downstream images which build on their base replace that banner so it is clear the image is not one of theirs and is not something they support. We satisfy this with our own Job Squire branding, which doubles as a nice touch on every startup.

The mechanism, per LinuxServer's container-branding guidance, is to bake a file named `branding` into the image at `/etc/s6-overlay/s6-rc.d/init-adduser/branding`, containing the text to display. The base image loads it automatically during init. One required detail: because Job Squire is a downstream image rather than a LinuxServer base image, the Dockerfile must set `ENV LSIO_FIRST_PARTY=false`, otherwise LinuxServer's init can overwrite our banner.

The banner is the JOB | SQUIRE wordmark with the vertical divider bar from the logo, above the official repository URL and the line that keeps us in good standing with LinuxServer by stating plainly that this is an independent project. Draft content for the `branding` file:

```
     _  ___  ____    |   ____   ___  _   _ ___ ____  _____
    | |/ _ \| __ )   |  / ___| / _ \| | | |_ _|  _ \| ____|
 _  | | | | |  _ \   |  \___ \| | | | | | || || |_) |  _|
| |_| | |_| | |_) |  |   ___) | |_| | |_| || ||  _ <| |___
 \___/ \___/|____/   |  |____/ \__\_\\___/|___|_| \_\_____|

           https://github.com/dellipse/job-squire
An independent project. Not affiliated with LinuxServer.io.
```

The vertical bar between JOB and SQUIRE matches the divider in the wordmark, and the URL and disclaimer are centered beneath it. It is pure ASCII so it renders correctly in any container log, and the exact art can be refined without affecting the mechanism. The repository URL tracks the official repo, so anyone reading a container log has a direct path back to the source.

### User and permissions model

The LinuxServer base brings a conventional `PUID`, `PGID`, and `UMASK` model, which aligns with how the current image already parameterizes the non-root user through build arguments and `data/.env`. The container continues to run the application as a non-root user, and the shared `/data` directory continues to be owned by that user so the bind-mounted host directory stays readable and writable across updates.

---

## 3. Configuration model: the `DEPLOY_MODE` preset, granular overrides, and the env matrix

### Two layers, unchanged

Job Squire already separates configuration into two layers, and this design keeps that split. Environment variables in `data/.env` are read once at container start and cover deployment shape: secrets, ports, URLs, schedule, and the like. In-app settings entered on the Settings page and stored encrypted in the database cover everything a running instance can change on the fly, such as AI providers and search targets. Deployment mode lives entirely in the first layer, the environment, because it must be known before the app fully starts.

### The `DEPLOY_MODE` preset

A single new variable, `DEPLOY_MODE`, is the only choice a normal setup makes. It takes `local` or `network`, defaulting to `local`. It is a convenience preset, not the thing the code actually reads. At startup it expands into a small set of granular flags, each of which has a sane default for the chosen mode. Presets keep the common case to one decision while the granular flags keep behavior correct and testable.

The important rule, and the reason this does not repeat the mistake of the legacy `AIConfig.mode` enum, is that the running code always reads the granular flags and never branches on `DEPLOY_MODE` itself. The mode only supplies defaults when a granular flag is unset. In shorthand:

```
mode          = env("DEPLOY_MODE", "local")               # the only value most users set
preset        = PRESETS[mode]                              # a table of sane defaults
trust_proxy   = env("TRUST_PROXY",          preset.trust_proxy)
secure_cookie = env("SESSION_COOKIE_SECURE", preset.secure_cookie)
# ... the app reads trust_proxy / secure_cookie / etc., never `mode`
```

Precedence is therefore simple and predictable: an explicitly set granular environment variable always wins; if it is unset, the preset default for the current mode fills it; the mode string is consulted only to pick which preset table to read. A normal user sets `DEPLOY_MODE` (or, more precisely, the CLI sets it from their answer to one setup question) and never sees a granular flag. A power user who sets `TRUST_PROXY` explicitly overrides just that one value without disturbing anything else.

### Where the preset expands

Expansion happens in two places, because two kinds of settings are involved. Application-level flags, the session cookie behavior, proxy-header trust, and the expected scheme, are computed in the Flask app factory from `DEPLOY_MODE` and its overrides, so the same logic applies no matter how the container is launched. Deployment-level choices, specifically which host interface the container's ports are published on and whether it joins a proxy network, are decided by the `job-squire` CLI when it generates the instance's env file and compose configuration, because those are container-launch concerns rather than app concerns. Both read the same `DEPLOY_MODE`, so the two layers always agree.

### The granular flags

| Flag | New or existing | Local default | Network default | Effect |
|---|---|---|---|---|
| `DEPLOY_MODE` | new | `local` | `network` | Selects the preset table. The only value most users set. |
| Host publish interface | existing vars, CLI-driven | `127.0.0.1:APP_HOST_PORT` / `127.0.0.1:MCP_HOST_PORT` (loopback only) | Shared proxy network (no host port), or `0.0.0.0` behind a firewall | How, and to whom, the container is reachable. |
| `TRUST_PROXY` | new | `0` (off) | `1` | When set, the app applies Werkzeug `ProxyFix` so `X-Forwarded-For/Proto/Host` are trusted and Flask-Limiter sees the real client IP. Must be off on loopback to prevent header spoofing. |
| `SESSION_COOKIE_SECURE` | existing | `false` | `true` | Secure cookies require HTTPS. Must be `false` on plain-HTTP loopback or the session cookie is silently dropped and login breaks. |
| `PUBLIC_URL` | existing | `http://localhost:APP_HOST_PORT` | `https://<host>` | Base URL used in emails and links. Network mode must be HTTPS. |
| `PUBLIC_MCP_URL` / `PUBLIC_MCP_HOST` | existing | loopback URL, or unset if MCP off | `https://<mcp-host>` | The MCP connector endpoint and its allowlisted host. |
| `SESSION_COOKIE_NAME` | existing (auto-derived) | derived from `INSTANCE_NAME` (e.g. `castelo_session`) | same | Keeps instances on a shared hostname from clobbering each other's sessions. Covered in Section 4. |
| MCP auth mode | new behavior | OAuth 2.0/PKCE, optional local static token | OAuth 2.0/PKCE only | Detailed in Section 5. |

The two genuinely new variables are `DEPLOY_MODE` and `TRUST_PROXY`. Everything else already exists in `examples/.env.example`; the design simply gives those existing knobs mode-aware defaults so a user no longer sets them by hand. In particular, `SESSION_COOKIE_SECURE` and the cookie-name derivation are already present today, so local mode is largely the current standalone behavior with sensible defaults, and network mode is the current SWAG behavior made explicit.

### Startup safety guard

Because a misconfigured network install is the dangerous case, the app validates its effective configuration at startup. If `DEPLOY_MODE` is `network` but `PUBLIC_URL` is not HTTPS or `TRUST_PROXY` is not set, that is unsafe. In `local` mode the app expects loopback publication and secure cookies off, so being bound to a non-loopback interface without a proxy in front, which would put a plain-HTTP instance on the network, is also unsafe. The design turns both of these easy mistakes into loud, early, actionable signals rather than silent problems.

Each check has a severity, and a hard rule: an unsafe condition is never left to a log file alone. Every message names the offending variable, its current value, why it is unsafe, and the exact change that fixes it.

| Severity | What happens | Where it is surfaced |
|---|---|---|
| Fatal (clearly unsafe) | The app refuses to start and exits non-zero. | The reason and the fix are written to the log **and** printed plainly to the console (stderr). The `job-squire` CLI that launched the container catches the non-zero exit and prints that same reason and fix on the command line, rather than a generic "container exited" message. |
| Warning (risky but runnable) | The app starts but flags the condition until it is corrected. | The message goes to the log, is echoed to the console at startup, **and** raises a persistent in-app banner. |

The in-app banner reuses the existing banner mechanism already used for worker-status and staleness warnings, so this is an extension of infrastructure that exists rather than something new. The banner is shown to the operator, states the same variable, reason, and fix, and stays visible until the underlying condition is resolved, at which point it clears on its own. The intent is that a person can hit an unsafe configuration from three different vantage points, the container log, the command line where they ran `job-squire`, and the application itself, and in every case be told plainly what is wrong and how to fix it.

---

## 4. Instance model and the cross-platform registry

### Every install is an instance

There is no special single-install path. Even the first install on a machine is created as a named instance and answers the same setup questions as any other. This is what lets local single, local multi-instance, and network installs share one flow: the difference between them is which mode preset applies and how many instances exist, not whether the machinery is different. An instance is the unit of isolation. Each one has its own database, its own uploads, its own encryption key, and its own two accounts, and instances share only the container image.

### The instance name is the primary key

Setup assigns each instance a unique name, and that name deterministically drives almost everything about the instance. The one thing the name does not drive is the encryption key, which is generated randomly at creation precisely because the name is not secret.

| What the name drives | Example for `castelo` | Why it must be unique per instance |
|---|---|---|
| Data directory | `.../job-squire/castelo/` | Separate SQLite database and uploads; the actual data isolation. |
| `SECRET_KEY` | generated randomly at creation, stored in the instance's env | Not derived from the name. Unique per instance so no instance can decrypt another's stored secrets. |
| `SESSION_COOKIE_NAME` | `castelo_session` | Browsers scope cookies by hostname, not port, so instances on `localhost` would otherwise clobber each other's sessions. This is the fix for the local multi-instance cookie collision. |
| Compose project name | `job-squire-castelo` | Namespaces container names so instances do not collide. |
| Port pair (local mode) | `8000` / `9000` | Distinct web and MCP host ports so instances do not fight over a port. |
| Hostname (network mode) | `castelo.example.com` | The reverse proxy routes each instance by its own hostname. |
| `PUBLIC_URL` / `PUBLIC_MCP_URL` | `http://localhost:8000` / MCP URL | Links in emails and the MCP connector endpoint. |

Because the name feeds cookie names, a compose project name, and a hostname label, setup sanitizes it to a safe slug (lowercase, alphanumeric and hyphen) and rejects a name that collides with an existing instance. The cookie-name derivation already exists in the app today, keyed off `INSTANCE_NAME`; this design makes it a first-class part of instance creation rather than something a person sets by hand, which closes the collision gap that the current `multi-instance.md` does not address.

### Keys and secrets are always independent

Every instance gets its own randomly generated `SECRET_KEY` at creation, and therefore its own Fernet-derived encryption for stored provider keys, SMTP password, and Anthropic key. There is no shared-secret path. If two people want to share a provider API key across their instances, they enter it in each instance separately; the application never moves secrets between instances. This keeps the blast radius of any one instance contained and matches the single-tenant-per-instance stance from Section 1.

### The cross-platform registry

For setup to detect existing instances and offer to import from them, and for the CLI to manage lifecycle, the machine needs a record of what instances exist. That record is a per-user registry file the `job-squire` CLI owns, in the conventional per-user config location for each operating system.

| OS | Registry location |
|---|---|
| macOS | `~/Library/Application Support/job-squire/instances.json` |
| Linux | `~/.config/job-squire/instances.json` (honoring `XDG_CONFIG_HOME`) |
| Windows | `%APPDATA%\job-squire\instances.json` |

The registry holds only non-secret metadata, never keys. It records each instance's name, mode, chosen runtime, data directory, ports or hostname, cookie name, public URL, and creation date. A representative shape:

```
{
  "version": 1,
  "instances": [
    {
      "name": "castelo",
      "mode": "local",
      "runtime": "podman",
      "data_dir": "/Users/dan/job-squire/castelo",
      "app_port": 8000,
      "mcp_port": 9000,
      "cookie_name": "castelo_session",
      "public_url": "http://localhost:8000",
      "created": "2026-07-11"
    }
  ]
}
```

The registry is per operating-system user, so two different OS logins keep separate lists, which is the natural boundary. The `SECRET_KEY` and every other secret stay in the instance's own env and database under its data directory, never in the registry.

### Setup and the import prompt

When setup runs, the CLI reads the registry. If other instances already exist, it offers to import basic settings from one of them, so a second person on the same machine does not start from a blank slate. Basic settings are non-secret configuration only: search titles, location, and radius; schedule hours and timezone; the list of enabled job-board providers by name; SMTP host and port; the selection of AI providers; and interface preferences. Secrets are excluded by default, meaning no API keys, no SMTP password, no `SECRET_KEY`. For the person who genuinely wants it, an explicit "also copy my keys" option can carry the provider keys over, but it is never the default and is always a deliberate choice.

### Port allocation and lifecycle bookkeeping

In local mode the CLI allocates the next free web and MCP port pair when it creates an instance and records the choice in the registry, so instances never collide on a port and a person never has to pick numbers. In network mode it records the hostname instead. When an instance is removed, the CLI updates the registry and asks whether to keep or delete that instance's data directory, so removing an instance never silently destroys someone's job-search history. These lifecycle operations are the CLI's responsibility and are detailed in Section 7.

---

## 5. Networking and security per mode, including MCP authentication

The two loopback modes and the network mode rest on fundamentally different trust boundaries. Local modes trust the machine itself: traffic never leaves the host, so the loopback interface is the security boundary. Network mode trusts an external reverse proxy and a firewall: the app assumes something in front of it terminates TLS and controls who can reach it. The design leans into each boundary rather than trying to make one mode behave like the other.

### Local modes: loopback is the boundary

Local installs serve plain HTTP on the loopback interface, and this is safe and warning-free because every modern browser treats `http://localhost`, `http://127.0.0.1`, and `http://*.localhost` as a secure context. Chrome and Edge have done so for years, Firefox since version 84, and Safari treats loopback the same. That means no "Not Secure" warning, no certificate prompt, no mixed-content blocking, and secure-context-only browser features working normally, all without a certificate. Because this is browser-engine behavior rather than operating-system behavior, it is identical on macOS, Windows, and Linux.

Two rules follow. First, setup and every printed link must use `localhost` or `127.0.0.1`, never the machine's LAN IP such as `192.168.x.x`. The loopback names get the secure-context treatment; a LAN address does not and would behave like ordinary insecure HTTP. Second, local mode publishes its ports on the loopback interface only, so the instance is not reachable from the network at all. In configuration terms, local mode runs with `SESSION_COOKIE_SECURE=false`, `TRUST_PROXY=0`, and loopback-only publication, exactly the granular defaults from Section 3. Multiple local instances are separated by distinct ports and the per-instance cookie names from Section 4, which is what prevents two instances on `localhost` from sharing a cookie jar.

If someone needs to reach a local instance from another device, there is one warning-free way to do it that keeps the app on loopback and exposes nothing publicly: a private Tailscale network, described in the next subsection. Anything beyond that, a publicly reachable address, crosses into network mode with its proxy and firewall.

### Reaching a local instance from your own devices (Tailscale)

A common and reasonable want is to reach a local instance from a phone while the computer stays at home. For a job seeker that is genuinely useful: brief on the way to an in-person interview and debrief immediately afterward while it is fresh, straight from a mobile device, without waiting to get back to the desk where the instance runs. Tailscale makes this possible without giving up the local security model or exposing anything publicly.

The mechanism is Tailscale Serve, not Funnel. Serve runs on the same host as the instance, terminates real HTTPS with a valid `device.tailnet.ts.net` certificate that Tailscale provisions, and forwards to `127.0.0.1`. The application never leaves loopback; Serve is simply a private, TLS-terminating front door reachable only by devices in the operator's own tailnet, and it can be tightened further with Tailscale ACLs. No ports are forwarded and nothing is published to the public internet. Funnel, by contrast, is public exposure and is treated as full network mode with every guard; this feature deliberately uses Serve.

Because Serve terminates TLS and forwards on the operator's behalf, an instance with tailnet access enabled adopts the network-mode application flags for those sessions even though it remains a local, unexposed install: secure cookies on, `TRUST_PROXY` on so it honors Serve's forwarded scheme and host, and `PUBLIC_URL` set to the `ts.net` name. It is therefore best thought of as local mode with a private Serve front door rather than a separate mode, reusing the network-mode flag preset with no public exposure. The MCP service benefits the same way, gaining a real `https://...ts.net` endpoint that is a cleaner connector target than a bare loopback port, with `PUBLIC_MCP_URL` and `PUBLIC_MCP_HOST` set to that name. Because the service is now reachable by other devices on the tailnet, MCP there should use OAuth rather than the local static token, consistent with the rule that reachability beyond the one machine prefers OAuth.

Two guardrails keep this safe: it uses Serve and never Funnel, and the app stays bound to loopback so Serve is the only way in. Configured this way, remote access from a trusted personal device does not weaken the local security model; it arguably improves on the plain-loopback default by adding a real certificate. The CLI can set this up as an optional capability, and the mechanics live in Section 7.

### Network mode: the proxy is the boundary

Network mode always sits behind an external reverse proxy, and the application never terminates TLS itself. This is a deliberate scoping decision: TLS, certificates, and public exposure are handled by mature tools built for it, SWAG, nginx, or any equivalent, and Job Squire speaks plain HTTP to that proxy over an internal network. We do not fold a proxy into our own image or take over TLS. What we do provide, described next, is optional automation so a non-technical operator is not left to wire up a proxy by hand. Network mode is still not considered configured without a working proxy in front.

The app's job is to behave correctly behind that proxy. With `TRUST_PROXY` set, it applies Werkzeug `ProxyFix` so it honors `X-Forwarded-Proto`, `X-Forwarded-Host`, and `X-Forwarded-For`, which is what makes secure cookies, correct external URLs, and accurate client IPs for rate limiting work. Secure cookies are on, `PUBLIC_URL` is HTTPS, and each instance is reached at its own hostname that the proxy routes to that instance. The MCP server keeps its existing DNS-rebinding protection by allowlisting `PUBLIC_MCP_HOST`. A firewall in front of the proxy is assumed; the app does not try to be the network's access control. The startup guard from Section 3 refuses the unsafe combinations, so a network instance cannot quietly come up without the proxy assumptions it depends on.

### Optional proxy provisioning

Because network mode requires a reverse proxy and a non-technical operator may not already have one, the `job-squire` CLI can provision it as part of setup rather than leaving it as a manual chore. There are two cases:

- **A proxy already exists.** If the machine already runs SWAG or another nginx-based proxy, the CLI generates the Job Squire proxy configuration for the web and MCP hostnames from the templates in `examples/nginx/`, drops it into the proxy's configuration directory, and reloads the proxy. It does not install a second proxy.
- **No proxy exists.** If the machine has no reverse proxy, the CLI can install and run a LinuxServer SWAG container, then generate and install the Job Squire configuration into it. SWAG bundles nginx, certbot, and fail2ban, so it is a self-contained way to stand up TLS on a machine that has nothing.

There is one thing the CLI cannot conjure: a domain and working DNS. TLS still depends on the operator supplying a hostname and a certificate validation path, whether HTTP or DNS based, so setup collects those inputs and configures SWAG with them, but it cannot substitute for actually owning a domain, though the free and low-cost options below make that cheap or free for personal use. The boundary from above still holds: we automate proxy setup as a convenience, and TLS still terminates at the proxy, but the proxy remains a separate, independently maintained component that is not part of the Job Squire image. The mechanics, installing the SWAG container, writing the configuration, and reloading, live in Section 7 with the other lifecycle operations.

### Free and low-cost domain and DNS options for personal use

Network mode needs a hostname and a certificate, but for personal use neither has to cost much or anything, and several options integrate directly with SWAG so the CLI can configure them. This applies only to network mode; a local install uses loopback and needs none of it.

- **DuckDNS (free, SWAG-native).** A free `yourname.duckdns.org` subdomain with built-in SWAG support and Let's Encrypt certificates, at no cost and with no domain purchase. The tradeoff is that DuckDNS issues either your main subdomain via HTTP validation or a wildcard via DNS validation, not both at once. This is the recommended zero-cost default, and the CLI can configure SWAG's DuckDNS mode directly.
- **A cheap domain with Cloudflare DNS (low cost, SWAG-native).** A domain from Cloudflare Registrar (about $10.44/yr at wholesale cost) or Porkbun (about $11/yr), paired with Cloudflare's free DNS, lets SWAG issue wildcard certificates through Cloudflare DNS-01 validation without exposing port 80. SWAG natively supports Cloudflare, Porkbun, DuckDNS, and many other providers, so a wide range of free and cheap choices work.
- **Tunnels, no port forwarding (free, different topology).** Tailscale Funnel is free for personal use and gives a `something.ts.net` hostname with automatic HTTPS, no domain to buy, and no inbound ports opened, though it caps at three funnels and is not meant for heavy public hosting. Cloudflare Tunnel is also free and opens no inbound ports but requires a domain on Cloudflare. Both expose the app through the tunnel provider rather than a locally facing proxy, so TLS terminates at the provider. This is a different network model than the SWAG reverse proxy, so the CLI suggests and documents these rather than treating them as the default network path; fully automating them is a candidate for later.

For personal use the recommendation is DuckDNS with Let's Encrypt as the free, CLI-configurable default, and Tailscale for anyone who wants private, secure remote access without exposing anything publicly, which fits the security and privacy focus. The split between what the CLI configures automatically and what it only documents is now settled (Section 7): DuckDNS and Tailscale Serve are fully automated, Cloudflare DNS-01 is semi-automated when the operator brings a domain and token, and Cloudflare Tunnel and other providers are documented.

### MCP authentication

MCP authentication follows the trust boundaries above. OAuth 2.0/PKCE is the primary flow and the default in every mode, and on network installs it is the only flow, because the claude.ai custom connector effectively requires it and because a public endpoint should not accept a long-lived shared secret. On local installs OAuth remains the default, with its callback and host defaulting to loopback so the flow completes on the same machine.

The one sanctioned alternative is a static bearer token, available on local installs only, for headless and non-browser MCP clients. OAuth's authorization-code flow assumes a browser to complete the consent redirect, which a local agent loop, a script, or a bridge like `mcp-remote` cannot easily provide. The application already supports exactly this pattern today, a static key used by a local agent such as Hermes, so the design formalizes rather than invents it. The token is generated by the CLI, stored encrypted with Fernet like every other secret, revocable, and never enabled on a network instance unless the operator explicitly turns it on. The default posture is therefore one clean primary flow everywhere, OAuth, with a proportionate, loopback-only escape hatch where OAuth's browser assumption breaks.

The token's concrete shape is settled. It is 256 bits of cryptographically random data, encoded URL-safe base64 with a short identifying prefix (`jsq_mcp_`) so it is recognizable in logs and by secret scanners. It is stored Fernet-encrypted at rest like every other secret and compared in constant time on each request. Its scope is the full set of MCP tools for the instance's single user, with no per-tool subdivision, because each instance is single-tenant; the server accepts it only on a loopback bind and rejects it on any network-reachable instance unless the operator has explicitly enabled it there. Exactly one token is active at a time: rotation regenerates it and immediately invalidates the previous value, available from both the CLI `configure` command and the in-app settings. There is no forced expiry by default, since the token is local and off unless enabled, but an optional time-to-live is supported, and the creation and last-used timestamps are recorded so a stale or unexpected token is easy to spot.

### Security posture at a glance

| Concern | Local (single or multi) | Network |
|---|---|---|
| Transport | Plain HTTP on loopback, a browser secure context | HTTPS terminated at an external proxy; plain HTTP app-to-proxy |
| Reverse proxy | None | Required; the CLI can install and configure SWAG, or configure an existing proxy |
| `SESSION_COOKIE_SECURE` | `false` | `true` |
| `TRUST_PROXY` / ProxyFix | off | on |
| Reachability | Loopback only; use `localhost`, never a LAN IP | Per-instance hostname behind proxy and firewall |
| MCP auth | OAuth by default; optional local static token for headless clients | OAuth only |
| Trust boundary | The machine | The proxy plus the firewall |

Across both, the app remains a two-account-per-instance application that is not hardened for multi-tenant use, which is why network mode is gated behind TLS, a proxy, and a firewall rather than exposed directly.

---

## 6. Container runtime selection and install

This section covers getting a machine ready: the bootstrap that lands the `job-squire` CLI, and how the CLI picks and installs a container runtime if one is not already present. The lifecycle operations the CLI performs afterward are Section 7.

### The bootstrap one-liner

Setup begins with a single command that downloads and installs the `job-squire` CLI from the official repository, then launches it to drive the rest of setup. The one-liner installs the CLI and nothing else; there is no separate installer with its own logic, and everything past this point is a CLI subcommand. It defaults to the latest released version and accepts a pin for a specific one.

| OS | Bootstrap | Version pin |
|---|---|---|
| macOS, Linux | `curl -fsSL https://raw.githubusercontent.com/dellipse/job-squire/main/bootstrap.sh \| sh` | `JOBSQUIRE_VERSION=<version>` before the command |
| Windows | `irm https://raw.githubusercontent.com/dellipse/job-squire/main/bootstrap.ps1 \| iex` | `$env:JOBSQUIRE_VERSION="<version>"` first |

The official repository is `github.com/dellipse/job-squire`. Latest is the default so the common case needs no version knowledge; the pin exists for reproducibility and for rolling back. Once installed, the CLI, invoked as `job-squire`, takes over and never asks the user to run a raw container command.

### Detect first, install only if needed

Before installing anything, the CLI checks for a container runtime that already works, looking for `docker`, `podman`, `orbstack`, and `colima` on the system. If it finds one that runs, it uses it and installs nothing. The design never installs a runtime over one the user already has, both out of respect for their setup and to avoid conflicts. Only when no working runtime is present does the CLI propose installing one, and it installs only with the user's consent.

### Per-operating-system defaults

When the CLI does need to install a runtime, it picks a default per platform, weighted toward the security-and-privacy posture from earlier while staying easy for a non-technical person. On Linux it also reads `/etc/os-release` to choose the right package path.

| OS | Default | Fallback | Why |
|---|---|---|---|
| Linux (local or server) | Podman, rootless | Docker Engine, if already present | Free including commercial, daemonless and rootless for the strongest posture, native on Fedora and RHEL, and in the repositories for Ubuntu 20.10+ and Debian 11+. Never Docker Desktop on a server. |
| macOS | Podman machine, CLI-automated | OrbStack | Free including commercial use with no license threshold, matching the security-and-privacy posture. The CLI scripts the `podman machine` setup so the rougher manual steps do not fall on the user. OrbStack is offered as a fallback for anyone who prefers its faster, lighter experience and is within its free personal-use terms. |
| Windows | Podman on WSL2, CLI-automated | Docker Desktop | Free including commercial and rootless, with the CLI scripting the `podman machine` setup so its rougher manual install does not fall on the user. Docker Desktop is the graceful fallback because it has the smoothest manual install if automation is unavailable. |

This makes the platform story consistent: Podman on Linux, Windows, and macOS, OrbStack available on Mac as an opt-in, and Docker only when it is already present or explicitly chosen. Because the image is multi-architecture, the same choice works on Intel and ARM without any per-architecture handling.

### Manual Docker instructions stay documented

Podman is the default, but Docker remains fully supported for environments that prefer or require it. The documentation keeps manual instructions for both Docker Engine, on Linux servers, and Docker Desktop, on macOS and Windows, so an operator who already runs Docker, or an organization standardized on it, has a clear supported path. The CLI's detect-and-reuse behavior means an existing Docker install is simply used as-is.

### The Windows WSL2 prerequisite

On Windows both Podman and Docker Desktop run their Linux containers inside a WSL2 virtual machine, so WSL2 is a shared prerequisite. The CLI checks for it and guides enabling it, which on current Windows is largely `wsl --install` plus a reboot. Microsoft's newer built-in WSL container feature (`wslc`) is worth watching as a future native option, but it is not usable here yet because it lacks Docker Compose support and is still pre-release; it is revisited when it reaches general availability with Compose.

### Licensing awareness

Two of the runtimes carry commercial-use thresholds. Docker Desktop is free only for companies under 250 employees and under $10M in revenue, and OrbStack requires a paid license for commercial use over $10k per year (verified 2026-07-11). Podman is free including commercial use with no threshold. The design sidesteps the issue rather than interrogating the user about it: Podman is the default on every platform, macOS included, so no install path silently steers anyone toward a paid product and setup never has to ask about company size. OrbStack stays available on macOS as an explicit opt-in for users who prefer it and are within its free personal-use terms, with its licensing stated plainly at that point of choice.

### Recording the choice

Once a runtime is selected or detected, the CLI records it in each instance's registry entry (the `runtime` field from Section 4), so later lifecycle commands know which runtime to drive for that instance without re-detecting each time.

---

## 7. Lifecycle and `job-squire` CLI touchpoints

This section defines what the `job-squire` CLI is responsible for at the deployment level, so the rest of the design has a clear home for the operations it keeps referring to. It is deliberately a list of touchpoints, not a full specification. The CLI's complete capability and interaction design, its exact command names and flags, its interactive setup experience, how it resolves versions from GitHub, and the fold-in of the existing `jobsquire-cli` project into this repository, is handled in its own dedicated session. Names shown here are illustrative and get finalized there.

### The CLI is the primary interface

The guiding principle is that the operator never needs to run a raw container command, and a non-technical user never has to think about the runtime at all. Whatever runtime is in play, Podman, Docker Engine, OrbStack, or Colima, the CLI drives it, reading the per-instance `runtime` field from the registry and translating to that runtime's compose invocation. `docker compose`, `podman compose`, and their differences stay hidden behind one consistent set of `job-squire` commands. This is the single strongest reason to fold the CLI into this repository, and it absorbs the repository's current `install.sh`, `update.sh`, and `uninstall.sh` into CLI subcommands. Primary does not mean exclusive, though: as described next, the containers stay fully manageable with the native tools for anyone who wants them.

### Direct runtime access remains available

Using the CLI is never mandatory. Each instance is an ordinary OCI container, run by the standard runtime from a standard compose file that the CLI generates; there is no proprietary wrapper format or hidden control plane. A user who prefers the native tools can manage an instance directly with `docker` or `podman` and their `compose` subcommands. The generated compose and env files live in a known per-instance location, so changing into that directory and running the runtime's compose commands works, and the containers are clearly named from the instance name (for example the compose project `job-squire-castelo`). The one thing to match is the runtime the instance was created with, recorded in the registry; on OrbStack and Colima the `docker` CLI is provided, so Docker commands work there as well.

There is a sensible division of labor. Operational and read-only commands, listing, logs, inspect, stop, start, restart, exec, and stats, are completely safe to run directly and will not put anything out of sync. Structural changes made outside the CLI, such as renaming a container, changing published ports, or deleting a volume, will not be reflected in the registry, which the CLI treats as the source of truth for instance metadata, so those are better done through the CLI. If a divergence does happen, `job-squire status` reports it, and the CLI can reconcile the registry with what is actually running. This keeps the design open and free of lock-in, consistent with the self-hosted ethos, while still letting the CLI be the easy default for everyone who wants it.

### Instance lifecycle operations

| Operation | What it does |
|---|---|
| Create | Runs setup: choose mode, name the instance, offer to import basic settings from an existing instance, allocate ports or set the hostname, generate a fresh `SECRET_KEY`, select or install a runtime, write the instance's env and compose files, register it, and bring it up. |
| Start / stop / restart | Brings the instance's single container up or down through the recorded runtime. |
| Status / list | Shows each instance from the registry and its health, using the aggregated container healthcheck from Section 2. |
| Update | Pulls the target image version, recreates the container, and relies on the app's additive boot-time migrations. Defaults to latest, accepts a pinned version, and supports rolling back to a previous version. Shutdown is WAL-safe because s6 forwards `SIGTERM` (Section 2). |
| Backup / restore | Produces a single portable archive of an instance in the user's home folder, capturing the whole data directory plus a manifest of registry and version information, and restores an instance from such an archive. Detailed below. |
| Remove | Tears down the instance, updates the registry, and asks whether to keep or delete that instance's data directory so history is never destroyed silently. |
| Configure | Adjusts an existing instance's settings, including turning tailnet access or the local MCP token on or off. |

### Backup and restore

Backup produces a single self-contained archive of one instance, written to the user's home folder, as a `.tgz` by default with a `.zip` option, named for the instance and a UTC timestamp (for example `job-squire-castelo-20260711T1830Z.tgz`). The archive contains the entire data directory verbatim, including any files the application itself does not manage, so nothing sitting in that folder is lost in a backup. Capture is WAL-safe: the CLI checkpoints the SQLite write-ahead log so the database is captured in a consistent state, reusing the existing WAL-safe backup approach rather than inventing a new one. By default it backs up one instance; an option can back up every registered instance in one run.

Alongside the data, the archive includes a manifest, `backup-manifest.json`, holding everything needed to restore faithfully: a backup-format version, the timestamp, the instance's full registry entry (name, mode, runtime, ports or hostname, cookie name, public URL), the image version the instance was running, the database schema or migration point, the CLI version, and checksums for integrity. The version information is what lets a restore bring the instance back up on a compatible image and carry the schema forward correctly.

Because a restorable backup must be able to decrypt the instance's stored secrets, the archive necessarily contains that instance's `SECRET_KEY` and its OAuth token store. Without the `SECRET_KEY`, the encrypted provider keys and SMTP password held in the database cannot be recovered, so it is included by design. That makes the archive sensitive, and because it carries the `SECRET_KEY`, encryption is not optional. Every backup archive is encrypted with a passphrase the user supplies at backup time, and an unencrypted archive is never written to disk. The key derivation and cipher are settled: the passphrase is stretched with Argon2id, the current OWASP-recommended passphrase KDF, and the archive is sealed with AES-256-GCM for authenticated encryption, so a corrupted or tampered archive fails loudly on restore rather than decrypting to garbage. A random salt and nonce plus the Argon2id parameters are stored in a small archive header. Both primitives come from the `cryptography` library the application already depends on, so no new dependency is added and nothing external such as the `age` binary is required; `age` remains a possible alternative only if a portable, interoperable archive format is ever wanted. The file is also given restrictive permissions. The tradeoff, which the CLI states plainly when creating a backup, is that the passphrase is required to restore and cannot be recovered if it is lost, so losing it means losing the backup.

Restore takes an archive and recreates the instance: it prompts for the archive passphrase and decrypts it, failing clearly if the passphrase is wrong, then verifies the checksums and backup-format compatibility, unpacks the data directory, restores the env including the `SECRET_KEY`, re-registers the instance from the manifest, keeps or reallocates ports and hostname as appropriate for the target machine, ensures a runtime is available, and brings the container up on a compatible image version, letting the app's additive migrations carry the schema forward if the target is newer. If an instance of the same name already exists, the CLI prompts to rename or overwrite rather than clobbering it silently.

### Provisioning touchpoints deferred here from earlier sections

Several earlier sections deferred their mechanics to the CLI. They land here as CLI responsibilities:

- **Runtime install (Section 6).** Detect an existing runtime and reuse it, or install the per-OS default with consent. The macOS default is Podman machine (CLI-automated), so no company-size question is asked; OrbStack is offered only as an explicit opt-in with its commercial-license terms shown at that point.
- **Proxy provisioning (Section 5).** For network mode, either generate the Job Squire configuration into an existing SWAG or nginx proxy and reload it, or install a LinuxServer SWAG container where none exists and configure it.
- **DNS and TLS (Section 5).** Auto-configure two paths and semi-automate a third. DuckDNS is the scriptable, guided default for network installs: collect the subdomain and token, put SWAG into DuckDNS mode, and obtain the Let's Encrypt certificate. Cloudflare DNS-01 is semi-automated: when the operator brings their own domain and API token, the CLI writes the SWAG Cloudflare configuration and issues the wildcard certificate, the one manual input being the domain and token the operator must supply. Cloudflare Tunnel and other SWAG DNS plugins are documented rather than automated, since they use a different topology or a long tail of provider-specific setup.
- **Tailscale Serve (Section 5).** For a local instance that wants private remote access, set up Tailscale Serve in front of the loopback service and flip that instance to the network-mode application flags, without any public exposure.
- **MCP authentication (Section 5).** Configure OAuth as the default, and on local installs optionally generate the static bearer token, stored encrypted and revocable, for headless clients.

### Surfacing failures

The CLI is also where the Section 3 startup guard becomes visible on the command line. When an instance refuses to start because of an unsafe configuration, the CLI catches the non-zero exit and prints the same reason and fix the app wrote, rather than a generic container error, so the operator sees an actionable message at the exact place they ran the command.

### What stays out of scope here

The finished command grammar, the interactive prompts and their wording, progress and error presentation, how the bootstrap resolves and verifies a version from GitHub, the container-lifecycle-manager details across runtimes, and the mechanics of merging `jobsquire-cli` into this monorepo including unifying the two projects' version schemes, are all the subject of the dedicated CLI session. Section 8 lists the fold-in and version-scheme unification among the open items.

---

## 8. Migration path and open questions

### From three containers to one

The move to a single container touches the image, the build, and the way services are defined, but not the application logic. The main pieces of work:

- **Base image and Dockerfile.** Replace the `python:3.14-slim` base with the LinuxServer `baseimage-alpine` base, pinned to a dated tag (currently the Alpine 3.23 line), installing Python via `apk add python3 py3-pip`, and add the branding file plus `ENV LSIO_FIRST_PARTY=false`. Note that this base ships Python 3.12, not 3.14; the app runs on 3.12 unchanged and 3.12 has the widest musllinux wheel coverage. The full lockfile has been verified to install on musl at 3.12 with binary wheels for every package and no source builds, `cryptography` included, so the musl risk is retired; pin `pydantic` and `pydantic-core` to keep resolution deterministic. The Debian-slim (with `tini` and s6 or supervisord) or LinuxServer Ubuntu fallback is retained only as a contingency.
- **s6 services.** Define the three longrun services (`web`, `worker`, `mcp`) under `/etc/s6-overlay/s6-rc.d/` with startup ordering, replacing the three compose services with one container that runs all three.
- **Multi-arch CI.** Add the QEMU and buildx setup to the pipeline so the image is built for `linux/amd64` and `linux/arm64`, where it currently builds a single architecture to GHCR.
- **Health.** Replace the three per-container healthchecks with the single aggregated container-level check.
- **Legacy compose removed once proven.** Keep the existing three-container compose in the repository only through migration, as a fallback while the single-container image is being validated. Once single-container is proven, the three-container compose is removed from the repository rather than maintained indefinitely. The separate maintenance-only `job-tracker` repository and its existing installs are unaffected.

### Adopting existing data

Existing installs already have a data directory and an `.env`. The CLI needs an adopt path that turns an existing data directory into a registered instance: derive the instance name (and therefore the cookie name) from the current `INSTANCE_NAME`, keep the existing `SECRET_KEY` so stored secrets stay decryptable, record the instance in the registry, and generate the single-container compose for it. Existing environment variables continue to be honored, so adoption is additive rather than a rewrite.

### Documentation that this supersedes

When this design ships, several current docs are replaced or updated, and this should be tracked so nothing goes stale:

| Doc | Disposition |
|---|---|
| `deployment.md` | Replaced by the network-mode and proxy-provisioning material here, plus the CLI runbook. |
| `multi-instance.md` | Replaced by the instance model and registry (Section 4), which also closes its cookie-collision gap. |
| `backup-restore.md` | Absorbed into the CLI backup and restore operation (Section 7), now a single encrypted archive. |
| `configuration.md` | Updated for `DEPLOY_MODE`, `TRUST_PROXY`, and the mode-aware defaults. |
| `architecture.md` | Updated for the single-container s6 topology. |
| User setup guide | Rewritten around the one-line bootstrap, the CLI, and the three modes. |

### Suggested sequencing

The work breaks into reviewable increments that each stand on their own:

1. Single-container image: s6 services, LinuxServer base, branding, multi-arch CI, aggregated healthcheck. Behavior otherwise unchanged.
2. Configuration: the `DEPLOY_MODE` preset, granular flags, mode-aware cookie and proxy behavior, and the startup safety guard with its three surfacing channels.
3. The `job-squire` CLI core: bootstrap, runtime detect and install, the instance registry, lifecycle commands, and backup and restore.
4. Provisioning: SWAG install and configuration, DuckDNS as the guided network default, Tailscale Serve for private local remote access, and MCP authentication setup.
5. Documentation supersession and the rewritten user setup guide.

### Consolidated open questions

These are the decisions still open or explicitly deferred, gathered in one place:

| Topic | Status |
|---|---|
| Setup licensing question | **Resolved (2026-07-11):** no question. Podman is the default on every platform including macOS, so setup never asks about company size and never steers anyone toward a paid product; OrbStack is an explicit opt-in on macOS with its licensing shown at the point of choice. See Section 6. |
| `jobsquire-cli` fold-in | Merging the existing `jobsquire-cli` project into this repository, and unifying the two version schemes (`0.1.0-sha` for the app, `0.1.0+sha` for the CLI). Deferred to the dedicated CLI session. |
| CLI command grammar and UX | Exact command names, flags, interactive prompts, progress and error presentation, and how the bootstrap resolves and verifies a version from GitHub. Deferred to the CLI session. |
| musl and Python version verification | **Resolved (2026-07-11):** verified. The pinned LinuxServer Alpine 3.23 base provides Python 3.12 (not 3.14), and the full lockfile resolves to musllinux binary wheels for all 61 packages with no source builds, `cryptography` included. Target the base's 3.12 and pin `pydantic`/`pydantic-core`. Debian-slim/Ubuntu fallback kept only as a contingency. See Section 2. |
| Auto-configure versus document | **Resolved (2026-07-11):** DuckDNS and Tailscale Serve are fully auto-configured; Cloudflare DNS-01 is semi-automated when the operator brings a domain and API token; Cloudflare Tunnel and other SWAG DNS plugins are documented only. See Sections 5 and 7. |
| Backup passphrase KDF | **Resolved (2026-07-11):** Argon2id for key derivation, AES-256-GCM for authenticated encryption, both from the already-present `cryptography` library, with salt, nonce, and parameters in the archive header. No new dependency; `age` noted only as a portability alternative. See Section 7. |
| Local MCP token details | **Resolved (2026-07-11):** 256-bit random token, URL-safe base64 with a `jsq_mcp_` prefix, Fernet-encrypted at rest, constant-time compared, full single-user tool scope, loopback-only unless explicitly enabled, one active token at a time with rotate-and-invalidate, no forced expiry (optional TTL), creation and last-used timestamps recorded. See Section 5. |
| Legacy compose lifetime | **Resolved (2026-07-11):** remove once single-container is proven. It is kept through migration as a validation fallback, then deleted from the repository rather than maintained indefinitely. See the Section 8 migration notes. |

### Resolved decisions, for the record

For completeness, the significant decisions this document settles: one codebase and one multi-arch image for all modes; single container on s6-overlay; three modes (local single, local multi-instance, network) driven by a `DEPLOY_MODE` preset over granular flags; every install is a named instance with a per-user registry; loopback is the local security boundary and an external proxy is the network one; Podman as the default runtime on every platform including macOS, with OrbStack an opt-in on Mac and Docker supported; a single-command bootstrap that lands the `job-squire` CLI as the front door; Tailscale Serve as the sanctioned private path to reach a local instance remotely; OAuth as the primary MCP auth with an optional local static token; and mandatory passphrase-encrypted backups using Argon2id and AES-256-GCM.

---

*End of draft. Awaiting full-document review.*
