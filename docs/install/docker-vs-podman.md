# Docker vs. Podman — Which Should You Use?

Both Docker and Podman run the same container images and both work with Job Squire. The choice is mostly about your security preferences and the Linux distribution you're running. This page explains the differences so you can make an informed decision.

---

## The short answer

| Situation | Use |
|---|---|
| New to containers, running on Mac or Windows | **Docker** |
| On Apple Silicon and want the fastest, lightest option | **OrbStack** — a drop-in Docker replacement; see the macOS section below |
| Using Debian, Ubuntu, or a generic VPS | **Docker** — easiest to install and most widely documented |
| Using Fedora, RHEL, CentOS Stream, or Rocky Linux | **Podman** — it's installed by default and better integrated |
| Privacy or security is a priority | **Podman** — no root daemon means a smaller attack surface |
| Running on a shared host or NAS | **Podman** — rootless operation isolates each user's containers |

---

## How they differ

### The daemon

Docker uses a persistent background service (`dockerd`) that runs as root. Every container you start is ultimately owned by that daemon. If the daemon is compromised, an attacker has root on the host.

Podman is daemonless. When you run `podman compose up`, it starts container processes directly under your own user account — no persistent privileged service. If a container is exploited, the blast radius is limited to what your user account can access.

### Root vs. rootless

Docker containers run as root by default inside the container, and the Docker daemon itself runs as root. You can configure rootless Docker, but it's an opt-in extra step.

Podman runs rootless by default. Containers run under your user ID, and the processes inside them are mapped to a range of unprivileged UIDs on the host. This is why Podman handles the `PUID`/`PGID` settings in Job Squire's `.env` the same way Docker does — both need to know which host user owns the data directory.

### Compose compatibility

Both runtimes use the same `docker-compose.yml` syntax. The only practical difference is the command:

| Runtime | Compose command |
|---|---|
| Docker | `docker compose` (v2 plugin) |
| Podman v4+ | `podman compose` (built-in) |
| Podman (older) | `podman-compose` (separate Python package) |

The install script detects which is available and uses the right one automatically.

### Auto-restart on reboot

Docker's daemon restarts automatically on boot (if you've enabled the Docker service), so containers with `restart: unless-stopped` come back up after a server reboot without extra steps.

Podman is daemonless, so it relies on systemd to restart containers. The install script handles this by:

1. Enabling `podman.socket` as a user systemd service.
2. Enabling *lingering* for your user account (`loginctl enable-linger`), which keeps your user's systemd session alive even after you log out.

If you skipped those steps or they failed, containers will not survive a reboot. Run these manually after installing:

```bash
systemctl --user enable --now podman.socket
sudo loginctl enable-linger $USER
```

### Image compatibility

Both runtimes pull from the same registries (Docker Hub, GitHub Container Registry, etc.) and run the same images. `ghcr.io/dellipse/job-squire:latest` works identically on both.

---

## Switching runtimes later

You can switch from Docker to Podman (or vice versa) at any time. Your data is in `data/` on the host and is not affected. Stop the running containers, install the other runtime, and re-run `install.sh` — it will ask which runtime to use and handle the rest.

---

## Running on macOS (CLI, no Desktop apps required)

Neither Docker Desktop nor Podman Desktop is required on macOS. Both runtimes are available as lightweight CLI tools via [Homebrew](https://brew.sh), each using a small Linux VM under the hood.

**Podman (recommended):**

```bash
brew install podman
podman machine init
podman machine start
```

The VM starts on demand. Podman runs rootless — no daemon, no root access needed.

**Docker via Colima (lightweight Docker Desktop alternative):**

```bash
brew install colima docker docker-compose
colima start
```

[Colima](https://github.com/abiosoft/colima) provides a Docker-compatible socket without requiring Docker Desktop or any privileged daemon. It starts a small Linux VM when you run `colima start` and exposes the standard Docker socket — transparent to any tool that uses `docker`.

**OrbStack (fastest Docker Desktop alternative on Apple Silicon):**

```bash
brew install --cask orbstack
```

Launch OrbStack once from Applications after installing so it sets up the `docker` and `docker compose` command-line tools. [OrbStack](https://orbstack.dev) is a drop-in Docker replacement built for macOS: it exposes the standard Docker socket, starts in seconds, and uses far less memory than Docker Desktop. Requires macOS 14 or later. Free for personal use; a paid license is required for commercial use in larger organizations. Direct download: https://orbstack.dev/download.

Which to choose? All three work identically for Job Squire. Podman is the simpler option if you're starting fresh and want rootless and daemonless. Colima is a natural fit if your other tools already expect a Docker socket. OrbStack is the fastest and lightest, with a polished menu-bar app, and is the easiest path if you'd rather not manage a VM by hand.

The `install.sh` script detects Homebrew and walks through any of these options automatically.

---

## Windows

On Windows, Docker Desktop is the simplest choice. Podman Desktop is also available.

- Docker Desktop: https://docs.docker.com/desktop/
- Podman Desktop: https://podman-desktop.io/

---

## Further reading

- Docker security overview: https://docs.docker.com/engine/security/
- Podman rootless containers: https://github.com/containers/podman/blob/main/docs/tutorials/rootless_tutorial.md
- OrbStack documentation: https://docs.orbstack.dev/
