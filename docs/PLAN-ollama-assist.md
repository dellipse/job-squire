# PLAN: Ollama Assist — Capability Detection, Guided Install, Model Selection

**Status:** Plan of record — agreed 2026-07-12. Not yet started.
**Goal:** Help the user determine whether their machine can reasonably run Ollama. If not, present it as an option anyway and explain concretely why it won't work on this machine. If it can, guide (or automate) installation and recommend specific models suited to Job Squire's workloads. Companion to `PLAN-ai-privacy.md`, where Ollama is the zero-egress privacy option.

---

## The Core Problem: Container Blindness

Job Squire runs in Docker. What the container can see about the host varies by platform:

| Platform | RAM/CPU visible in container | GPU visible |
|---|---|---|
| Linux (native Docker/Podman) | Host values via `/proc` — mostly trustworthy | NVIDIA only with explicit passthrough; usually invisible |
| macOS (OrbStack/Docker Desktop) | The **VM's** allocation, not the Mac's | Apple Silicon GPU: never |
| Windows (Docker Desktop/WSL2) | The **WSL2 VM's** allocation | Usually invisible |

An in-app-only detector would tell a 64 GB M4 Mac owner they have 8 GB. Detection therefore uses three sources with strict precedence:

1. **CLI host detection (authoritative).** `job-squire ollama check` runs on the host: OS/arch, physical RAM, CPU cores, Apple Silicon (`sysctl`/`system_profiler`), NVIDIA VRAM (`nvidia-smi`), AMD (`rocm-smi`), whether Ollama is already installed/running. Writes `host_capabilities.json` into the instance's `DATA_DIR`, which the web app reads (CLI already knows each instance's data dir via the instance registry — `PLAN-deployment-modes.md`).
2. **In-app best-effort auto-detect.** Reads `/proc/meminfo`, `/proc/cpuinfo`, and virtualization signals. On Linux-native it may be trusted; when VM/Docker-Desktop signals are present it must NOT assert numbers — it only pre-fills the questionnaire with "we can't see your real hardware from inside the container."
3. **Questionnaire fallback.** Three questions in the wizard: OS, approximate RAM, GPU (Apple Silicon / NVIDIA + VRAM / none). Pre-filled from source 2 where honest.

Freshness: `host_capabilities.json` carries a timestamp; the app shows when detection last ran and offers the CLI command to refresh.

---

## Capability Tiers → Model Recommendations

Job Squire's local-AI workloads: **triage** (score new jobs 1–10 with short rationale, runs 3×/day, needs speed) and **analysis** (resume/kit tailoring, follow-up drafts, weekly review — needs quality and instruction-following). `AIProviderConfig` already has separate `triage_model` / `analysis_model` fields; recommendations fill both.

Context is a real constraint: profile + job description exports run several thousand tokens. Ollama's default context window is too small — the provider adapter must set `num_ctx` (≥ 8192, 16384 where RAM allows) or analysis inputs get silently truncated.

| Tier | Hardware | Triage model | Analysis model | Notes |
|---|---|---|---|---|
| **Not reasonable** | < 8 GB RAM, no GPU | — | — | See messaging below |
| **Entry** | 8 GB RAM, CPU-only | Qwen3 4B (Q4) | Phi-4-mini or Gemma 3 4B (Q4) | Works but slow (CPU inference); set expectations: analysis may take minutes. Suggest cloud provider for analysis + local for triage as a hybrid. |
| **Capable** | 16 GB RAM, or GPU with 8 GB VRAM | Qwen3 4B | Llama 3.3 8B or Qwen3 8B (Q4) | The sweet spot for most self-hosters. |
| **Strong** | Apple Silicon 16 GB+, or 12 GB+ VRAM, or 32 GB RAM | Llama 3.3 8B | Qwen3 14B class (Q4) | Apple Silicon unified memory + Metal makes Macs disproportionately good here. |
| **Workstation** | 24 GB+ VRAM or 48 GB+ unified | Qwen3 8B | 30B-class (Q4) | Optional; diminishing returns for these tasks. |

Q4_K_M quantization is the default recommendation across tiers. **Exact model tags must be re-verified against the Ollama library at implementation time and reviewed periodically** — this table reflects the mid-2026 landscape and will age. Keep the tier→model mapping in one data structure (`app/ollama_assist.py`), not scattered in templates.

### "Not reasonable" messaging

When the machine falls below Entry tier, do not hide the option — explain it:

> "Ollama lets you run AI entirely on your own machine, so your data never leaves it. This machine has {X} GB RAM and no GPU we can detect. Models small enough to run here produce noticeably poor results for resume and cover letter work, and would take several minutes per response. Ollama remains an option if you upgrade or run it on another machine on your network (enter its address below). For now, a free cloud tier (Gemini, Groq, OpenRouter) will give you much better results — with automatic identifier redaction protecting your data."

The "another machine on your network" path is first-class: the Ollama provider's base URL field already supports it; the wizard offers a connectivity test against any URL.

---

## Installation Flows

**Web app (guidance):** per-OS instructions with official links only — macOS: download from ollama.com (native app, Metal support; do NOT run Ollama inside Docker on a Mac — it loses GPU). Windows: official installer. Linux: official install script shown for review, or Ollama's own Docker image with `--gpus` for NVIDIA hosts. Then: pull commands for the tier's two models, and how the app reaches the host (`host.docker.internal` on Mac/Windows; `--add-host=host.docker.internal:host-gateway` on Linux — compose files gain this by default). Note: host Ollama must listen beyond localhost for the container to reach it (`OLLAMA_HOST=0.0.0.0` with a firewall note, or platform-specific host-gateway routing) — document per-OS.

**CLI (automation with consent):** `job-squire ollama setup` runs the full chain, each step individually confirmed:
1. `check` (above) → report tier and recommended models
2. Install via the **official** installer for the OS (never a bundled copy)
3. Start/verify the service
4. `ollama pull` the two recommended models (show sizes first — multi-GB downloads)
5. Write the provider config into the instance (Ollama provider row: base URL, triage/analysis models, `num_ctx`), enabled and ranked
6. End-to-end test: one round-trip prompt through the app's provider adapter

`--dry-run` prints everything it would do. Every step also printed as the manual command, so the CLI doubles as documentation.

**Verification in-app:** wizard/Settings "Test Ollama" button — checks reachability, lists pulled models, flags missing recommended models with the exact pull commands, runs a 1-token generation to confirm inference works.

---

## Integration Points

- **Onboarding step 3** (`PLAN-onboarding.md`): the AI setup step leads with the privacy framing; "Local AI (Ollama)" branch runs detection → tier verdict → guided install or the not-reasonable explanation.
- **Settings → AI providers:** same detection panel available permanently, not just during onboarding.
- **`PLAN-ai-privacy.md`:** when the active provider chain is Ollama-only, the UI shows the zero-egress badge; redaction defaults off for local (`redact_local`).
- **Worker:** triage via local model may be slow on Entry tier — the search-run pipeline already runs async; add a per-run timeout suited to CPU inference rather than cloud latencies.

---

## Testing

- Tier mapper: unit tests over the capability matrix (RAM/VRAM/platform combinations → expected tier and models).
- Detection honesty: VM-signal fixtures assert the in-app detector refuses to report numbers on Mac/Windows Docker.
- CLI `check` on each platform in CI where runners allow; `setup --dry-run` golden output.
- Provider adapter: `num_ctx` actually sent; truncation warning when input exceeds it.

---

## Honest Limitations

- Detection can't see everything (eGPUs, unusual ROCm setups); the questionnaire override is always available.
- Model recommendations decay — revisit at each release; the tier table is data, not doctrine.
- Entry-tier quality is real: local privacy vs. output quality is a genuine trade-off below 16 GB, and the UI should say so plainly rather than overselling local AI.
