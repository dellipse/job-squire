# Session-starter prompt: Job Squire deployment open questions

Copy the block below into a new session to work through the open questions from the deployment design.

---

I'm Daniel, working on Job Squire, a self-hosted job-search app (repo github.com/dellipse/job-squire, in the "Modern Job Hunt" monorepo). We finished a deployment design document at `job-squire/docs/PLAN-deployment-modes.md`. Read it first; it is the plan of record and nothing in it is implemented yet.

This session's goal is to resolve or advance the OPEN QUESTIONS listed in Section 8 of that document. Do not design the full `job-squire` CLI here; that has its own separate session. Work these items:

1. Setup licensing question. Decide whether setup asks a single company-size question to steer the macOS OrbStack default toward Podman when a paid license would apply, or defaults silently to the free-and-secure choice. Recommend one.
2. musl + Python 3.14 verification. Determine whether the full requirements/lockfile installs on the pinned LinuxServer Alpine base (musl), with particular attention to `cryptography` (Fernet), and whether the base's `apk` provides Python 3.14. If it does not, recommend a base or fallback (Debian-slim + tini + s6/supervisord, or LinuxServer Ubuntu base).
3. Auto-configure versus document. Finalize which DNS and remote-access options the CLI configures automatically (leaning DuckDNS and Tailscale Serve) versus only documents (Cloudflare Tunnel, other SWAG DNS providers).
4. Backup passphrase KDF. Choose the key-derivation function and archive-encryption format for the mandatory encrypted backups (candidates: age, scrypt, argon2id). Encryption is required, never optional.
5. Local MCP token details. Define token format, scope, and rotation for the local-only static bearer token (behavior is already decided: local only, Fernet-encrypted, revocable, off by default).
6. Legacy three-container compose lifetime. Keep it indefinitely, or remove once single-container is proven.

Explicitly deferred to the separate CLI session (note, do not solve here): the `jobsquire-cli` fold-in into this monorepo and unifying the two version schemes (`0.1.0-sha` app vs `0.1.0+sha` CLI), and the full CLI command grammar, interactive UX, and GitHub version resolution.

How to work: review the document, then take each open item in turn. For each, briefly note assumptions and any information you would need, research current facts where relevant (search before asserting present-day facts like pricing, package versions, or library support), give a clear recommendation, and update Section 8 and any affected sections of the document as decisions are made. Keep me in the loop and confirm before large changes. No em-dashes or other AI-tells in any drafted text. Be concise and direct.

Useful context in memory: "Job Squire deployment plan", "Job Squire runtime selection", "Job Squire single-container / s6", "versioning-convention". Reference date: the document was drafted 2026-07-11.

---
