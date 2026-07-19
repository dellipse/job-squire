# PLAN: Ollama Assist — Capability Detection, Guided Install, Model Selection

**Status:** CLI side implemented 2026-07-16 (`job-squire ollama check` / `job-squire ollama setup`,
ops/ollama_assist.py). Same day: the `num_ctx` app-side gap this doc originally left open is now
closed too — `AIProviderConfig.num_ctx` (migration in `app/__init__.py`), a context-capacity check in
`call_with_fallback` (`app/ai.py`) that skips an under-provisioned provider instead of risking a
silently truncated prompt, and `triage_model`/`num_ctx` fields in the Settings provider forms (they
existed on the model but had no form field at all until now). Also same day: prompt chunking, so a
task doesn't just fail outright when nothing in the provider chain has enough context — see "Prompt
chunking" below. Remaining web-app integration points below (onboarding step 3, the guided-install
panel proper, in-app "Test Ollama" button) are not started.
**Goal:** Help the user determine whether their machine can reasonably run Ollama. If not, present it as an option anyway and explain concretely why it won't work on this machine. If it can, guide (or automate) installation and recommend specific models suited to Job Squire's workloads. Ollama is the zero-egress privacy option: running AI entirely on the user's own machine, so nothing about their job search leaves it.

---

## The Core Problem: Container Blindness

Job Squire runs in Docker. What the container can see about the host varies by platform:

| Platform | RAM/CPU visible in container | GPU visible |
|---|---|---|
| Linux (native Docker/Podman) | Host values via `/proc` — mostly trustworthy | NVIDIA only with explicit passthrough; usually invisible |
| macOS (OrbStack/Docker Desktop) | The **VM's** allocation, not the Mac's | Apple Silicon GPU: never |
| Windows (Docker Desktop/WSL2) | The **WSL2 VM's** allocation | Usually invisible |

An in-app-only detector would tell a 64 GB M4 Mac owner they have 8 GB. Detection therefore uses three sources with strict precedence:

1. **CLI host detection (authoritative).** `job-squire ollama check` runs on the host: OS/arch, physical RAM, CPU cores, Apple Silicon (`sysctl`/`system_profiler`), NVIDIA VRAM (`nvidia-smi`), AMD (`rocm-smi`), whether Ollama is already installed/running. Writes `host_capabilities.json` into the instance's `DATA_DIR`, which the web app reads (CLI already knows each instance's data dir via the instance registry).
2. **In-app best-effort auto-detect.** Reads `/proc/meminfo`, `/proc/cpuinfo`, and virtualization signals. On Linux-native it may be trusted; when VM/Docker-Desktop signals are present it must NOT assert numbers — it only pre-fills the questionnaire with "we can't see your real hardware from inside the container."
3. **Questionnaire fallback.** Three questions in the wizard: OS, approximate RAM, GPU (Apple Silicon / NVIDIA + VRAM / none). Pre-filled from source 2 where honest.

Freshness: `host_capabilities.json` carries a timestamp; the app shows when detection last ran and offers the CLI command to refresh.

---

## Capability Tiers → Model Recommendations

Job Squire's local-AI workloads: **triage** (score new jobs 1–10 with short rationale, runs 3×/day, needs speed) and **analysis** (resume/kit tailoring, follow-up drafts, weekly review — needs quality and instruction-following). `AIProviderConfig` already has separate `triage_model` / `analysis_model` fields; recommendations fill both.

Context is a real constraint: profile + job description exports run several thousand tokens. Ollama's default context window is too small (2048 tokens, regardless of what the underlying model actually supports) — and, confirmed against https://docs.ollama.com/api/openai-compatibility, the OpenAI-compatible endpoint app/ai.py calls has **no per-request field for context size at all**. The only supported method is baking it into the model via a Modelfile (`FROM <model>` / `PARAMETER num_ctx <n>`, then `ollama create`) and referencing the derived model's name. `job-squire ollama setup` does exactly this (`derive_context_model` in ops/ollama_assist.py) — the tier table's `num_ctx` (≥ 8192, 16384 where RAM allows) is what gets baked in, not sent per-call.

| Tier | Hardware | Triage model | Analysis model | Notes |
|---|---|---|---|---|
| **Not reasonable** | < 8 GB RAM, no GPU | — | — | See messaging below |
| **Entry** | 8 GB RAM, CPU-only | `qwen3:4b` | `gemma3:4b` | Works but slow (CPU inference); set expectations: analysis may take minutes. Suggest cloud provider for analysis + local for triage as a hybrid. |
| **Capable** | 16 GB RAM, or GPU with 8 GB VRAM | `qwen3:4b` | `qwen3:8b` | The sweet spot for most self-hosters. |
| **Strong** | Apple Silicon 16 GB+, or 12 GB+ VRAM, or 32 GB RAM | `qwen3:8b` | `gemma4:12b` | Apple Silicon unified memory + Metal makes Macs disproportionately good here. |
| **Workstation** | 24 GB+ VRAM or 48 GB+ unified | `qwen3:8b` | `qwen3.6:27b` | Optional; diminishing returns for these tasks. |

Q4_K_M quantization is the default recommendation across tiers. Tags re-verified against
https://ollama.com/library on **2026-07-16** (superseding the mid-2026 placeholders this table
originally shipped with — Llama 3.3 8B and "30B-class" are no longer the sharpest picks now that
Qwen3.6 and Gemma 4 are out). **These will age again — re-verify periodically.** The mapping lives
in one data structure, `job_squire_cli/job_squire_cli/ops/ollama_assist.py`'s `TIER_TABLE`
(CLI side, implemented) — not `app/ollama_assist.py`, which doesn't exist yet: the CLI is a
separately-packaged, Flask/SQLAlchemy-free install (pyproject.toml only depends on `click` +
`cryptography`), so it carries its own copy rather than importing from the app package. When the
web-app integration below is built, either import this table from a small shared/vendored module or
keep the app's own copy in sync by hand — don't let the two silently drift.

### "Not reasonable" messaging

When the machine falls below Entry tier, do not hide the option — explain it:

> "Ollama lets you run AI entirely on your own machine, so your data never leaves it. This machine has {X} GB RAM and no GPU we can detect. Models small enough to run here produce noticeably poor results for resume and cover letter work, and would take several minutes per response. Ollama remains an option if you upgrade or run it on another machine on your network (enter its address below). For now, a free cloud tier (Gemini, Groq, OpenRouter) will give you much better results — with automatic identifier redaction protecting your data."

The "another machine on your network" path is first-class: the Ollama provider's base URL field already supports it; the wizard offers a connectivity test against any URL.

---

## Installation Flows

**Web app (guidance):** per-OS instructions with official links only — macOS: download from ollama.com (native app, Metal support; do NOT run Ollama inside Docker on a Mac — it loses GPU). Windows: official installer. Linux: official install script shown for review, or Ollama's own Docker image with `--gpus` for NVIDIA hosts. Then: pull commands for the tier's two models, and how the app reaches the host (`host.docker.internal` on Mac/Windows; `--add-host=host.docker.internal:host-gateway` on Linux — compose files gain this by default). Note: host Ollama must listen beyond localhost for the container to reach it (`OLLAMA_HOST=0.0.0.0` with a firewall note, or platform-specific host-gateway routing) — document per-OS.

**CLI (automation with consent) — implemented 2026-07-16:** `job-squire ollama check [NAME]` and
`job-squire ollama setup NAME` (docs/job-squire-cli.md has the full option reference). `setup` runs
the chain below, confirming before installing anything:
1. `check` (host detection) → report tier and recommended models; writes `host_capabilities.json`
   into `NAME`'s `data/` dir (visible to the container) unless `--dry-run`
2. Install via the **official** channel for the OS — Homebrew formula on macOS, the official install
   script on Linux, winget on Windows (never a bundled copy); skipped entirely if Ollama already works
3. Start/verify the service (macOS: `brew services start ollama`; Linux/Windows install their own service)
4. `ollama pull` the two recommended (or `--triage-model`/`--analysis-model`-overridden) base tags
5. Derive a context-sized model from each base tag (`ollama create <tag>-ctx<n>` from a generated
   Modelfile — see the "Context is a real constraint" note above for why this is the only way to set
   context size against the endpoint app/ai.py calls). `--num-ctx` overrides the tier's recommendation;
   `--skip-derive` writes the bare base tags instead (Ollama's 2048-token default then applies, and
   app/ai.py's capacity check treats the provider as unconfigured — no truncation guard).
6. Write the provider config into the instance's `ai_provider_configs` table directly via `sqlite3`
   (base URL, the *derived* triage/analysis model names, `num_ctx`, rank, enabled) — `num_ctx` requires
   the app-side migration (`app/__init__.py`); an older instance image gets a clear, actionable error
   telling the operator to `job-squire update` first, not a raw `sqlite3.OperationalError`.
7. End-to-end test: a direct round-trip prompt against the Ollama API (not literally through the
   app's own provider adapter, which runs inside the container this host-side CLI doesn't import) —
   the in-app "Test Ollama" button, once built, remains the authoritative check.

`--dry-run` prints everything it would do without installing, pulling, deriving, or writing anything.
`--skip-pull`/`--skip-derive`/`--skip-test` opt out of individual steps. `--yes` skips the install
confirmation only.

**App side, implemented alongside the CLI 2026-07-16 (not deferred, per the "Honest Limitations"
principle of not letting a known gap linger silently):**
- `AIProviderConfig.num_ctx` (nullable Integer) — metadata describing what a provider's configured
  model was actually built with; never sent as a request parameter (there's nowhere to send it).
- `call_with_fallback` (app/ai.py) estimates prompt size (a `len(text)//4` heuristic, deliberately not
  a real tokenizer dependency) against `num_ctx` before calling a provider with one set, and skips to
  the next provider in the ranked chain — exactly like an unmet `use_for_triage`/`use_for_analysis`
  flag already does — instead of sending a prompt Ollama would silently truncate. If every eligible
  provider gets skipped this way, the resulting error names the reason explicitly (distinct from "no
  providers configured"), so an unattended worker run logs something actionable rather than a generic
  failure — the risk this whole feature exists to close for automation (auto-triage, weekly review,
  etc.), not just interactive use.
- Settings → AI providers: `triage_model` and `num_ctx` are now editable fields on both the add and
  edit provider forms (`triage_model` existed on the model since the multi-provider work but had no
  form field at all — a separate, adjacent bug found and fixed in the same pass: `call_with_fallback`
  was reading it from nowhere in the call path either, so a CLI-configured triage_model was silently
  ignored in favor of the analysis model on every triage call).

**Prompt chunking, added 2026-07-16 (goes one step further than "skip to the next provider"):**
Skipping an under-sized provider is correct but conservative — if nothing in the ranked chain has
enough room (the expected case for a single local Ollama instance with no cloud fallback), the task
just fails with a clear error rather than running at all. Chunking closes that gap: when
`call_with_fallback` exhausts every provider on capacity grounds, it now raises `ContextCapacityError`
(a `RuntimeError` subclass, so nothing that already catches `RuntimeError`/`Exception` needs to
change), which task-level code can catch to shrink the prompt and retry instead of giving up. The full
single-shot prompt is always tried first — chunking is strictly a fallback, so a provider with enough
context still gets the higher-quality single-pass result every time; chunking only fires when the
configured model genuinely can't fit it.
- `run_auto_triage` / `run_followup_drafts`: independent per-job batches, so a capacity failure just
  means "retry with a smaller batch" — `_call_batched_with_capacity_shrink` halves recursively down to
  one job per call. No reassembly needed since each job is already scored/drafted independently and
  applied to the DB per-batch.
- `run_weekly_review` / `run_rejection_analysis`: one aggregate prompt over the whole pipeline, so a
  capacity failure means real map-reduce — `_run_chunked_or_single` splits the job list into chunks and
  runs the same analysis prompt on each, then `_reduce_partial_analyses` makes one more call to
  synthesize the partial results into a single coherent review (that reduce call only ever sees the
  already-condensed partial summaries, not raw job data, so it stays small regardless of pipeline size).
  Real tradeoff, stated plainly rather than hidden: cross-job pattern detection can be weaker across
  chunk boundaries than a genuine single-pass review sees the whole pipeline at once. Because of that,
  whenever this path runs the returned `overall_summary` is prefixed with an explicit note that the
  analysis was chunked due to the model's context window — visible wherever the review is read (the UI,
  the saved `AIInsight`, the weekly email digest), not just in the worker's log line.

**Verification in-app:** wizard/Settings "Test Ollama" button — checks reachability, lists pulled models, flags missing recommended models with the exact pull commands, runs a 1-token generation to confirm inference works.

---

## Integration Points

- **Onboarding step 3** (`PLAN-onboarding.md`): the AI setup step leads with the privacy framing; "Local AI (Ollama)" branch runs detection → tier verdict → guided install or the not-reasonable explanation.
- **Settings → AI providers:** same detection panel available permanently, not just during onboarding.
- **AI privacy:** when the active provider chain is Ollama-only, the UI shows the zero-egress badge; redaction defaults off for local (`redact_local`).
- **Worker:** triage via local model may be slow on Entry tier — the search-run pipeline already runs async; add a per-run timeout suited to CPU inference rather than cloud latencies.

---

## Testing

- Tier mapper: unit tests over the capability matrix (RAM/VRAM/platform combinations → expected tier and models). Implemented: job_squire_cli/tests/test_ollama_assist.py.
- Detection honesty: VM-signal fixtures assert the in-app detector refuses to report numbers on Mac/Windows Docker. (Web-app detector not yet built — CLI-side detection has no such ambiguity since it always runs on the host.)
- CLI `check` on each platform in CI where runners allow; `setup --dry-run` golden output. Implemented for the pure-logic pieces (tier classification, install plans, Modelfile derivation, provider-config writes) with fully injected subprocess/network fakes; real per-OS CI runners not set up.
- Provider adapter: `num_ctx` actually sent; truncation warning when input exceeds it. Superseded by what actually shipped: `num_ctx` is never "sent" (no such field exists on the endpoint) — instead `fits_in_context`/`estimate_tokens` (app/ai.py) gate whether a provider is attempted at all, tested in tests/test_ai_context_capacity.py (capacity skip, ranked-chain fallback, the triage_model fix, and the Settings form round-trip).
- Prompt chunking: tests/test_ai_chunking.py — the `_call_batched_with_capacity_shrink` and
  `_run_chunked_or_single` helpers directly (no-shrink, shrink-and-succeed, give-up-at-min-chunk,
  non-capacity exceptions still propagate unshrunk), `_reduce_partial_analyses` in isolation, and
  full `run_auto_triage`/`run_weekly_review`/`run_rejection_analysis` integration runs against a
  fake provider call that fails above a size threshold — including a regression guard that a
  provider with enough room gets the plain single-pass result with no chunking note at all.

---

## Honest Limitations

- Detection can't see everything (eGPUs, unusual ROCm setups); the questionnaire override is always available.
- Model recommendations decay — revisit at each release; the tier table is data, not doctrine.
- Entry-tier quality is real: local privacy vs. output quality is a genuine trade-off below 16 GB, and the UI should say so plainly rather than overselling local AI.
- Chunked weekly review / rejection analysis is a real quality tradeoff, not a free win: a map-reduce
  pass over isolated chunks can miss a pattern that only becomes visible when jobs from different
  chunks are seen side by side (e.g., the same rejection reason recurring across chunk boundaries).
  It's still strictly better than the alternatives — an outright failure, or (pre-`num_ctx`-check) a
  silently truncated prompt — but it is not equivalent to a genuine single-pass review, which is why
  it's always the fallback, never the default, and why the returned summary says so explicitly rather
  than presenting a chunked result as if it were the full-quality one.
