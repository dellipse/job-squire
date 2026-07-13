# Setup Guide

This guide walks through setting up Job Squire from nothing — no programming experience assumed,
no prior knowledge of Docker, containers, or the command line beyond copying and pasting a couple
of commands into a terminal. It works the same way on macOS, Windows, and Linux.

Everything after the first command is driven by the `job-squire` command-line tool. You don't
need to hand-edit configuration files, wire up a database, or know what a "container" is — the
tool asks a short series of questions and does the rest.

---

## Before you start

You'll need:

- A macOS, Windows, or Linux computer.
- 15–20 minutes for the initial setup, plus a few more minutes per job board you want to connect.
- Optional, but recommended before your first automated search: a free API key from at least one
  job board (the setup steps below tell you exactly where to get one).

You do **not** need to install Docker, Python, or anything else yourself first — the setup tool
checks for what it needs and offers to install it for you, asking before it changes anything.

---

## Step 1: Install the `job-squire` tool

Open a terminal (**Terminal** on macOS, **PowerShell** on Windows, your usual shell on Linux) and
run one of these:

**macOS or Linux:**

```bash
curl -fsSL https://raw.githubusercontent.com/dellipse/job-squire/main/bootstrap.sh | sh
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/dellipse/job-squire/main/bootstrap.ps1 | iex
```

This single command downloads and installs the `job-squire` tool from the official GitHub
repository and hands off to it. It doesn't touch anything else on your computer. If you'd rather
install a specific version instead of the newest one, that's also supported — see
[`deployment.md`](deployment.md#installing-the-cli) — but for a first install, just run the
command above as-is.

The tool then checks whether you already have a container runtime (the piece of software that
actually runs Job Squire, isolated from the rest of your computer). If you don't have one, it
offers to install **Podman** — a free, open-source option that works the same way whether you're
an individual or a business — and explains exactly what it's about to do before doing it. If you
already have Docker installed, it uses that instead and installs nothing new.

---

## Step 2: Create your instance

```bash
job-squire create
```

This asks you a few questions:

1. **Instance name** — a short label for this install, like `mystuff` or your own name. If you're
   only ever going to run one copy of Job Squire, this doesn't matter much; pick anything.
2. **Deployment mode** — almost everyone wants `local` here. This runs Job Squire entirely on your
   own computer, reachable only from that computer, with no setup beyond this. The other option,
   `network`, is for putting Job Squire on a server that other devices reach over the internet —
   see ["Which mode do I want?"](#which-mode-do-i-want) below if you're not sure, and
   [Setting up network mode](#setting-up-network-mode-optional) later in this guide if you pick it.
3. **Admin password** — leave blank to have one generated for you (it will be printed once, so
   save it somewhere).
4. **A second account, for the job seeker** — if the person searching for a job is a different
   person than the one running the setup (for example, a family member helping out), you can set a
   separate login for them. Otherwise one account covers both roles.

That's it. `job-squire create` then brings the instance up and prints the address to open in your
browser — for local mode, something like `http://localhost:8080`.

If you ever want to run a second, completely separate copy (for a second person, keeping their
data fully apart from yours), just run `job-squire create` again with a different name — see
[`multi-instance.md`](multi-instance.md).

---

## Step 3: Sign in

Open the address `job-squire create` printed. Sign in with `admin` and the password from Step 2
(or whichever account you set up for the job seeker).

---

## Which mode do I want?

| If you're... | Pick |
|---|---|
| One person, running this on your own laptop or desktop | **local** |
| Two or more people sharing one machine, each wanting their own separate pipeline | **local** (just run `job-squire create` again for the second person) |
| Putting this on a server so it's reachable from anywhere, or running it for a small group | **network** |

Local mode needs nothing else — no domain name, no certificate, no reverse proxy. It's reachable
only from the computer it's running on, which every modern browser treats as a fully secure
connection with no warnings. If you later want to check your pipeline from your phone without
setting up network mode, see ["Reaching a local instance from your phone"](#reaching-a-local-instance-from-your-phone-optional)
below — it's a middle ground that stays fully private.

Network mode is for a server that other devices reach over the internet, and it always requires a
domain name and a certificate for encryption (HTTPS). The setup tool can handle most of that for
you too — see [Setting up network mode](#setting-up-network-mode-optional) below.

---

## Step 4: Set up your search

Sign in and open **Settings**, then work through each tab.

**Search tab**

- Enter job titles (one per line).
- Set your location as `City, ST` (e.g. `Austin, TX`) if you're in the US — ZIP codes and street
  addresses aren't accepted, since the job search APIs need a city and state. Outside the US, any
  non-empty location works.
- Set a search radius, an optional minimum salary, and how old a posting can be before it's
  ignored.

**Sources tab**

For each job board you want to use:

1. Click the "get a key" link and sign up (all are free).
2. Paste the key into the field.
3. Tick "Use this source" and save.

Adzuna + Jooble is a good starting pair with solid coverage for most US metro markets. The Muse and
Jobicy need no key at all and are worth turning on regardless (The Muse is on by default on a new install).

**Email tab**

Fill in your email provider's SMTP settings and turn on notifications, then click **Send test
email** to confirm it works. This is what sends you a digest whenever new jobs are found.

**Candidate Profile tab**

Write or upload your master profile — the resume/background summary every application kit is
built from — and add any supporting documents (existing resumes, recommendation letters,
certificates) to the document library.

**AI tab** (optional, but where most of the value is)

Two independent switches:

- **Automatic Features** — turns on background AI work: scoring new jobs automatically, drafting
  follow-up emails, and a weekly strategy review. Needs at least one AI provider — several (Gemini,
  Groq, OpenRouter) have a free tier that's enough for typical use.
- **MCP Connector** — lets you talk to your pipeline directly from a Claude conversation: "build me
  a kit for this job," "what's overdue for follow-up," and so on. See Step 5 below to connect it.

Both can be on at the same time, and neither is required — copying your pipeline into Claude by
hand always works with no setup at all.

**Application Kit tab**

Set a salary floor (default $60,000); postings below it are flagged so you don't spend effort on
underpaying roles.

---

## Step 5: Connect Claude (optional)

1. On **Settings → AI tab → MCP Connector**, turn the connector on, give it a name, and save.
2. In Claude: **Settings → Connectors → Add custom connector**, and paste the URL shown on the
   Settings page.
3. Claude opens a sign-in page — use the job seeker's Job Squire login (not the admin account).
4. Once connected, "Open in Claude" buttons appear throughout the app, on job pages and the
   Settings AI tab, and Claude can read and update your pipeline directly.

If you're connecting a non-browser tool instead of Claude itself (a local agent script, for
example), see ["MCP authentication"](deployment.md#mcp-authentication) in the deployment runbook
for the alternative static-token method.

---

## Step 6: Run your first search

1. Go to **Settings → Search tab**.
2. Click **Run search now**.
3. New postings show up under the `Saved` status on the Jobs page, and a digest email goes out if
   anything was found.

From here, the automated schedule takes over — three times a day on weekdays, once on weekends, by
default (Settings → Search tab shows and lets you change this).

---

## Keeping it running

```bash
job-squire status NAME     # is it healthy?
job-squire update NAME     # move to the newest version
job-squire backup NAME     # make an encrypted copy of everything
job-squire stop NAME
job-squire start NAME
```

Run these any time — they're the whole day-to-day toolkit. See
[`deployment.md`](deployment.md) for the complete command reference, and
[`backup-restore.md`](backup-restore.md) for the backup/restore walkthrough — worth doing once,
soon after your first real setup, so you know it works before you ever need it.

---

## Reaching a local instance from your phone (optional)

If you use [Tailscale](https://tailscale.com) (a free personal VPN between your own devices), you
can check your pipeline from your phone while the instance keeps running on your computer at home,
without exposing anything to the public internet:

```bash
job-squire tailscale enable NAME
```

This gives the instance a real, certificate-secured address reachable only by your own signed-in
devices. See [`deployment.md`](deployment.md#reaching-a-local-instance-remotely-without-going-to-network-mode)
for details.

---

## Setting up network mode (optional)

Network mode is for running Job Squire on a server so it's reachable by hostname rather than only
from one computer — useful for a small group, or if "local" isn't the right shape for your setup.
It always needs a domain name (a free one from [DuckDNS](https://www.duckdns.org) works fine for
personal use) and a certificate, and the setup tool automates most of both:

```bash
job-squire create --mode network --hostname castelo.example.com
job-squire proxy castelo                                              # sets up the reverse proxy
job-squire dns duckdns castelo --subdomain castelo --token <your-duckdns-token>  # domain + certificate
```

Full walkthrough, including what to do if you already have a domain on Cloudflare instead of
DuckDNS, is in [`deployment.md`](deployment.md#network-mode-the-reverse-proxy).

---

## Local development (for contributors)

If you're working on Job Squire's own source code rather than just running it, you don't need the
CLI or a container at all:

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

`fcntl`-based DB locking is Linux-only; on macOS it degrades gracefully for single-process dev use.

---

## Something not working?

See [`troubleshooting.md`](troubleshooting.md) for the most common issues and their fixes.
