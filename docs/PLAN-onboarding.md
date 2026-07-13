# PLAN: First-Run Onboarding (Getting Started)

**Status:** Phase 1 SHIPPED 2026-07-12 (`app/onboarding.py`). Phase 2 (AI resume interview) SHIPPED 2026-07-12 — not yet released (staged, VERSION not bumped). Deviation from plan: `/setup` kept as the routines redirect (actively linked from the dashboard); the checklist got its own nav entry instead. Crash fix 2026-07-13: resume interview was hitting gunicorn `WORKER TIMEOUT` against slow providers (see "Follow-up" section below) — needs an async redesign before wide release, not just the timeout stopgap.
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
6. **Job board providers** — walk through `PROVIDERS` registry. Zero-key providers (The Muse, Jobicy) are pre-suggested so the first search always works; keyed providers (SerpApi, Adzuna, Jooble, ZipRecruiter, USAJOBS) get signup-link guidance from the existing registry metadata.
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

## Phase 2 — Resume interview & profile generation (SHIPPED, staged)

- **Research pass:** done via web search (ATS single-column/reverse-chronological parses most reliably; one page under 5 years experience, two pages at 5+; quantify 70%+ of bullets; skills section should mirror the target posting's exact wording; omit photo/age/marital/health signals). Encoded as `prompts.RESUME_BEST_PRACTICES`, shared by all three transports so the resume comes out consistent regardless of which AI ran the interview.
- **Interview flow**, three transports:
  - **API mode:** `ai.run_resume_interview_turn(history, candidate_name)` — one question per round trip. There's no chat-history param in this codebase's provider dispatch (`call_with_fallback` sends one system + one user message), so each turn resends the transcript as plain text and the model is instructed to emit a `===RESUME_READY===` / `===PROFILE_FACTS===` sentinel block when it has enough to write the resume. Wizard step: `GET/POST /getting-started/profile/interview` (`app/templates/resume_interview.html`), state carried in a hidden `history_json` field — no new DB table, no server-side session.
  - **MCP mode:** `prompts.resume_builder_mcp_prompt(connector)` — an on-demand routine (not one of the 5 scheduled slots) that has Claude read existing profile/assets, interview conversationally, then call the new `save_resume_draft(resume_markdown, profile_facts)` MCP tool.
  - **Manual mode:** `prompts.resume_interview_manual_prompt()` — self-contained copy-paste prompt; the user runs it in any free AI chat and pastes the finished resume into a textarea on the profile step, which posts to `POST /getting-started/profile/resume-draft`.
- All three write through one function, `onboarding.save_resume_draft()`: upserts the single `CandidateAsset(kind="Resume")` — a new kind, added distinct from the existing uploaded `"Base Resume"` kind so re-running the interview replaces its own output without touching an uploaded file — and appends `profile_facts` to `candidate_profile.md` under a "From resume interview" heading.
- Step 4b placeholder replaced in `getting_started_step.html` with the three options plus an existing-draft indicator.
- Tests: `tests/test_onboarding.py::TestResumeInterview` (save/replace/reject-blank, route persistence, admin gate, no-provider redirect, turn-progression + completion via a mocked `call_with_fallback`). Full suite (223 tests) and `ruff check` pass.
- Not yet done: bumping `VERSION` (CI auto-releases on that), a CHANGELOG entry beyond what's staged, and the "Open in Claude ↗" wiring was reused as-is (relies on the existing `claude_buttons_enabled` setting, same as other routine prompts).

---

## Follow-up — resume interview should move to the async pattern (noted 2026-07-13)

`onboarding.resume_interview()` → `ai.run_resume_interview_turn()` (`app/ai.py:1407`) still calls
`call_with_fallback()` directly on the request thread — one full round trip per turn, blocking a
gunicorn worker for the whole model call. Every other slow-AI-call route in this codebase (triage,
followup, weekly_review, build_kit, ats-gap, score-fit, draft-followup) was already moved off this
pattern after the job 1162 ats-gap incident (2026-07-01): background thread + `_TaskStatus` +
poll via `GET /ai/task/<run_id>` (see `main.py:691-700` for the writeup, `_run_single_job_ai_task`
for the shared helper). The interview route was never migrated.

This bit us for real on 2026-07-13: two `WORKER TIMEOUT` crashes mid-interview against a slow
keyless/free provider, response size growing turn over turn (transcript is resent as flat text
each round — `call_with_fallback` has no chat-history param, see Phase 2 notes above). Gunicorn's
`--timeout` was bumped 60→180s as a stopgap (`root/etc/s6-overlay/s6-rc.d/web/run`), but that just
buys headroom; it doesn't fix the design.

Worth a real pass before this ships broadly:
- Migrate the interview turn to the background-thread + poll pattern like every other AI call site,
  so a slow turn can't take a worker down with it.
- Consider actually holding a live chat session with the provider instead of "resend the whole
  transcript as one flat user message, reload the page, repeat" — e.g. a real multi-turn `messages`
  array and/or streaming tokens back to the browser, closer to how this assistant or Hermes Agent
  hold a conversation than to the current page-reload-per-turn wizard step.

---

## Out of Scope (for now)

- Resume rendering to .docx/.pdf (markdown only; kits already handle tailoring).
- Multi-tenant onboarding — app remains two-user.
- In-wizard SMTP setup (link to Settings instead; not essential to first value).
