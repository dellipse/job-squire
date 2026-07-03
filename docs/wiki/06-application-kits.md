# Application Kits

An application kit is a single file containing your master profile, the job details, and a full step-by-step prompt. Paste it into Claude and it works through a complete application package for that specific job.

## What Claude produces from a kit

Working through six steps in order, Claude returns:

**Step 0 — Fit assessment.** A one-line verdict (Strong Fit / Partial Fit / Stretch) with flags for salary below your minimum, missing hard requirements, overqualification, and location or work-mode conflicts. If the verdict is "Consider skipping", Claude stops and asks whether to continue.

**Step 1 — Reference documents.** Claude reads your master profile and any uploaded resume files accessible to it.

**Step 2 — Live research.** Claude fetches the current job posting (if a URL is available) and researches the company: what they do, recent news, salary benchmarks for the role in your location, and how the posted salary compares to your minimum target. A salary warning appears prominently at the top if the role looks low.

**Step 3 — The application package:**
- An ATS keyword analysis table (which key terms are Present, Absent, or Partial in your profile, and what to do about each gap).
- A tailored resume, built around the posting's language and keywords.
- A cover letter (under 300 words), referencing a specific current detail about the company.
- An application email to send with your resume.
- Three follow-up emails: a check-in after 5-7 days with no response, a thank-you within 24 hours of an interview, and a polite check-in a week after the interview.
- Five likely interview questions with answer frameworks built from your real experience.
- Three smart questions to ask the interviewer, drawn from the actual posting.
- Two LinkedIn outreach messages (one short connection request, one direct message).

**Step 4 — Save artifacts.** Claude saves a Markdown file and a Word document of the full kit output.

**Step 5 — Push to Job Squire (MCP mode).** If the MCP connector is active and the kit came from a tracked job, Claude saves the kit back to the job record and sets a follow-up reminder 6 days out.

Everything follows your writing rules: no em-dashes, no AI-sounding filler, only real facts from your profile.

## Building a kit

**From a job you are already tracking:** open the job and click **Application kit**. In MCP mode, a **Build kit in Claude** button also appears — click it to open a pre-loaded Claude chat with context already loaded.

**For a brand-new posting:** click **Application kit** in the top menu. Select a tracked job from the dropdown to auto-fill the form, or paste a new job title, company, and posting text. Tick **Save to Job Squire** to add the job at the same time.

Then open Claude (claude.ai), start a new chat, and paste the downloaded file as your first message. Claude works through all six steps and returns everything ready to use. Save the finished resume and cover letter back onto the job as attachments.

## ATS gap analysis (standalone)

If you have the Anthropic API key configured and AI mode set to **API**, each job detail page also shows an **ATS Gap Analysis** button. This runs a focused keyword gap check against your profile without building the full kit — useful for a quick read on whether your resume is likely to pass an automated screen before you apply.

## Style rules applied to all kit output

- Write like a real person. Warm, direct, professional.
- No em-dashes anywhere. Use commas, periods, or rewrite the sentence.
- No AI-tell phrases: no "I am thrilled", "leverage", "passionate about", "in today's fast-paced world", "delve", "tapestry".
- Only real numbers and achievements from your profile. Nothing fabricated.
- Contact details exactly as they appear in your profile.
