# PLAN: AI Privacy — PII/SPI/PHI Redaction & Rehydration

**Status:** Plan of record — agreed 2026-07-12. Not yet started.
**Goal:** No personal identifiers, sensitive personal information, or health information is transmitted to any AI provider. The app substitutes placeholders before transmission and rehydrates real values into results. Applies to **all three AI paths**: API provider chain, manual-mode copy/paste exports, and MCP tool responses.

---

## Design Principles

1. **Identifiers vs. substance.** Identifiers (name, email, phone, address, URLs, third-party names) contribute nothing to AI reasoning — always tokenized, zero quality loss. Substance (work history, employers, titles, skills) is what the AI reasons over — sent intact by default, pseudonymized only in optional strict mode.
2. **SPI/PHI rule:** *if it belongs in the final application, tokenize it; if it should not reach employers at all, warn and coach it out.* Work authorization or security clearance for a federal role → tokenize + rehydrate. Health conditions, disability disclosures, age signals (old graduation years), marital status, photos → flag with coaching guidance to remove from the source documents; block transmission of flagged spans until resolved.
3. **Local detection only.** Never use a cloud AI to detect PII (circular). No NER libraries — Presidio/spaCy add hundreds of MB and conflict with the single-container Alpine/s6 plan (`PLAN-deployment-modes.md`).
4. **Single choke point.** All outbound AI text passes through one module; no AI call site builds its own redaction.
5. **Ollama is the strongest answer.** For zero-egress users, local AI already exists. Onboarding and Settings present "local AI (Ollama) — your data never leaves your machine" as the privacy-first option. Redaction applies to cloud providers; for Ollama it is skippable (config toggle, default off for local).

---

## Agreed Decisions

| Decision | Choice |
|---|---|
| Employer names / locations | Sent intact by default (tailoring quality); optional **strict mode** pseudonymizes them (`{{ORG_1}}`, `{{LOC_1}}`) with a quality warning in the UI. |
| SPI/PHI | Mixed: warn + coach removal when it doesn't belong in application documents; tokenize + rehydrate when it legitimately does. |
| MCP mode | Redacted identically. Claude reads placeholder text via tools, writes drafts containing placeholders, app rehydrates on save/render. No "trusted Claude" exception. |

---

## Detection Strategy (no ML dependencies)

Two complementary passes:

1. **Known-values pass.** The app already holds the candidate's exact name, email, phone, address, and profile URLs (profile, SMTP config, contacts table). Exact/normalized match-and-replace of known values — far more reliable than NER for the values that matter most. Contacts (recruiters, references) are third-party PII: their names/emails/phones tokenize the same way, keyed per contact (`{{CONTACT_2_NAME}}`).
2. **Pattern pass.** Regexes for emails, phone numbers, SSNs, street addresses, DOB-like dates, ZIP+4, URLs containing usernames. SPI/PHI keyword heuristics (condition/medication vocabulary, "disabled", "married", graduation-year arithmetic) feed the warn-and-coach flags, not silent replacement.

Placeholder format: `{{PII:KIND_N}}` — distinctive, survives model round-trips better than bracket styles. Rehydration uses a lenient matcher (tolerates whitespace/case mangling); any placeholder left unresolved in output triggers a visible warning, never silent failure.

---

## Architecture

New module `app/privacy.py`:

```
redact(text, context)   -> (redacted_text, mapping)   # mapping: placeholder -> real value
rehydrate(text, mapping) -> (text, unresolved: list)
scan_spi(text)          -> [flags]                    # warn-and-coach findings
```

- **Mapping storage:** persisted per-exchange, Fernet-encrypted like all other secrets (`crypto.py`). For MCP, mapping must survive across the read→write round trip: store the active mapping server-side keyed to the session/job; MCP write tools rehydrate before persisting.
- **Choke points wired:** `ai.py` (`run_api_analysis`, triage, follow-up drafts, weekly review, rejection analysis), `prompts.py` export builders (manual mode — exported JSON/text is pre-redacted since it goes to a cloud chat), `mcp_server.py` (all read-tool responses redacted; all write tools rehydrate).
- **Config:** new fields on `AIConfig` — `redaction_enabled` (default on), `strict_mode` (default off), `redact_local` (default off, applies to Ollama/LiteLLM-local). Settings UI explains each with plain-language privacy implications.
- **SPI/PHI flags UI:** findings surface on the profile/asset pages and in the onboarding resume step as coaching items ("Your resume includes your graduation year, which reveals your age — recommended: remove"). Flagged spans are excluded from outbound AI text until the user resolves or explicitly overrides per-flag.

---

## Interaction With Onboarding (`PLAN-onboarding.md`)

- Onboarding step 3 (AI setup) presents the privacy posture: Ollama as the zero-egress option; for cloud providers, an explanation that identifiers are replaced with placeholders automatically.
- The Phase 2 resume interview runs through the same choke point — the manual-mode interview prompt and any API/MCP interview traffic is redacted like everything else.
- SPI/PHI coaching doubles as resume-quality guidance in the interview flow (modern resume practice: no photos, no age, no marital status, no health info).

---

## Testing

- Unit: known-values replacement (including partial/formatted variants of phone numbers), each regex class, strict-mode org/location pseudonymization, rehydration round-trip, lenient matcher against deliberately mangled placeholders, unresolved-placeholder warning path.
- Integration: each AI path (API mock, manual export, MCP tool response) asserts zero known identifiers in outbound payloads — a "no-leak" test class that runs against real profile fixtures.
- Property-style: redact→rehydrate is identity on text containing no flagged spans.

---

## Honest Limitations (document these for users)

- Regex + known-values cannot catch every conceivable identifier in free text (e.g., a nickname never entered into the app). Strict mode narrows this; Ollama eliminates it.
- Redaction protects *transmission to AI providers*. It does not anonymize the final documents — those are supposed to identify the candidate.
- Job posting text arrives from public boards and is not treated as candidate PII.
