# PLAN: First-Run Onboarding (Getting Started)

**Status:** Phase 1 SHIPPED 2026-07-12 (`app/onboarding.py`). Phase 2 (AI resume interview) not started. Deviation from plan: `/setup` kept as the routines redirect (actively linked from the dashboard); the checklist got its own nav entry instead.
**Problem:** A fresh install drops the user on an empty Dashboard with no guidance. Nothing tells them to add a resume, set search targets, configure AI, or connect job boards.

---

## Agreed Design Decisions

| Decision | Choice |
|---|---|
| Delivery | **Checklist + wizard.** Persistent "Getting Started" card on the Dashboard; each item opens a focused guided step. Re-entrant by design — skip/revisit is automatic. |
| Persona | **Ask up front.** First step asks "Are you setting this up for yourself, or helping someone else?" and adjusts copy + account guidance accordingly. |
| Resume creation without AI | **Manual-mode prompt.** App generates a self-contained interview prompt the user pastes into any free AI chat; they paste the finished markdown resume back. Consistent with the existing Manual AI mode. |
| Build approach | **Phased.** Phase 1 = checklist shell + wiring of existing capabilities. Phase 2 = AI resume interview and profile generation. |

---

## Step Order (reordered from original request)

AI setup was moved **before** resume creation: the conversational resume interview
(step 4b) needs an AI provider or at least the manual-mode prompt flow, and job
analysis later in the flow needs it too.

1. **Welcome & persona** — self vs. helping someone else; sets wizard copy.
2. **Accounts** — offer the optional second account (helper/coach in self mode; job seeker in helper mode). Requires a new in-app route: accounts are currently seeded only from `ADMIN_PASSWORD` / `USER_PASSWORD` env vars (`_seed_users()` in `__init__.py`).
3. **AI setup** —
   - "Will you use AI for job scoring, resumes, cover letters?"
   - Privacy posture up front: Ollama presented as the zero-egress option; for cloud providers, explain automatic identifier redaction (see `PLAN-ai-privacy.md`).
   - Ollama branch: hardware capability detection → tier verdict → guided install and model recommendations, or an honest "why not on this machine" explanation (see `PLAN-ollama-assist.md`).
   - If yes: Claude account? (Free/Pro/Max → MCP connector setup, existing Settings → Claude tab). Other subscriptions (ChatGPT, Gemini, xAI, Nous Portal, ...)? Free/low-cost API routes (Gemini free tier, Groq, OpenRouter, GitHub Models, Ollama local)? Walk through provider config (existing multi-provider chain UI).
   - If no: explain the reduced feature set (no auto-triage, no drafts, no weekly review; Manual mode still works) and continue.
4. **Resume & profile** —
   a. "Have a resume?" → upload as `CandidateAsset` (kind: Resume).
   b. "Need to create one?" → Phase 2 interview flow (see below).
   c. Online profiles (LinkedIn etc.) → stored in `candidate_profile.md`.
   d. Certifications, recommendation letters → upload as `CandidateAsset`s.
   e. Everything textual lands in `candidate_profile.md` in `DATA_DIR` — the existing file the AI flows and MCP `get_candidate_profile` already read. **No new file store.**
5. **Search targets** — job titles/queries, location ("City, ST"), radius, include-remote toggle (new — see schema), target salary (`SearchConfig.min_salary` + `KitConfig.fit_salary_floor`), max posting age.
6. **Job board providers** — walk through `PROVIDERS` registry. Zero-key providers (Dice, The Muse, Jobicy) are pre-suggested so the first search always works; keyed providers (SerpApi, Adzuna, Jooble, ZipRecruiter, USAJOBS) get signup-link guidance from the existing registry metadata.
7. **First search & next steps** — trigger a search run, show results; if AI is enabled, run triage on the results; end with a "here's what to do next" summary (review scored jobs, apply, set follow-ups).

Every step is individually skippable; skipped steps stay visible on the checklist until completed or the card is dismissed.

---

## Phase 1 — Checklist shell + wiring (ship first)

Fixes "dumped into the Dashboard" using capabilities that already exist.

### New model
```
OnboardingState (singleton, id=1)
  persona          "self" | "helper" | NULL
  steps_json       {"accounts": "done"|"skipped"|"pending", ...}
  dismissed        bool
  completed_at     datetime nullable
```
Additive migration in `_run_migrations()`, seeded like other singletons.

`SearchConfig.include_remote` (bool, default True) — new column. Gates the
remote-only providers (Jobicy; The Muse remote listings) and is exposed in the
wizard and Settings. `Job.work_mode` already exists for display/filtering.

### First-run detection
Fresh installs are unambiguous: `SearchConfig` seeds with `enabled=False` and
empty `titles`/`location`, no `CandidateAsset` rows, no `candidate_profile.md`.
Show the Getting Started card on the Dashboard whenever `OnboardingState.dismissed`
is false and steps remain. A "Getting Started" link stays in the nav (or Settings)
permanently so the walkthrough can be revisited anytime.

### Routes & templates
- `GET /getting-started` — checklist overview page (also rendered as a Dashboard card).
- `GET/POST /getting-started/<step>` — one focused page per step. Steps mostly
  wrap existing forms/routes (asset upload, search config, provider credentials,
  AI provider config) with guidance copy, rather than duplicating them.
- Repurpose the vestigial `/setup` redirect (`main.py:1508`) to point at `/getting-started`.
- New admin-only route: create/enable the second account in-app (username,
  display name, role, password) — currently env-var-only.

### Phase 1 step coverage
Steps 1, 2, 3, 4a/4c/4d, 5, 6, 7 — everything except the resume-creation interview (4b), which shows a "coming soon: create a resume with AI help" placeholder pointing at upload for now.

### Tests
Route tests for checklist rendering/skip/dismiss/state persistence; migration test for `OnboardingState` + `include_remote`; account-creation route tests (auth: admin only). Respect existing coverage floors.

---

## Phase 2 — Resume interview & profile generation

- **Research pass first:** review current (2026) resume best practices — ATS-friendly formatting, quantified achievements, skills sections, length norms — and encode the findings into the interview prompt template in `prompts.py`.
- **Interview flow**, three transports matching the existing AI modes:
  - **API mode:** multi-turn interview driven through the configured provider chain; app renders the Q&A in the wizard step.
  - **MCP mode:** a new Claude routine ("Onboarding / Resume Builder") in `prompts.py`; Claude interviews conversationally and writes back via `save_candidate_profile` + a new `save_resume_draft` (or reuse `save_kit`).
  - **Manual mode:** generate a copy/paste interview prompt (asks about work history, education, skills, certifications, achievements); user runs it in any free AI chat and pastes the finished markdown resume back into the app.
- Output: markdown resume stored as a `CandidateAsset` (kind: Resume) + profile facts merged into `candidate_profile.md`.
- Wire step 4b into the checklist, replacing the placeholder.

---

## Out of Scope (for now)

- Resume rendering to .docx/.pdf (markdown only; kits already handle tailoring).
- Multi-tenant onboarding — app remains two-user.
- In-wizard SMTP setup (link to Settings instead; not essential to first value).
