# Candidate Profile and Documents

Everything Claude writes is built from your **master profile** plus the documents you have on file. Both live under **Settings → Candidate Profile**.

## Master profile

The master profile is a plain-language Markdown document covering your background, skills, experience, target roles, salary expectations, and location preferences. It is the single source of truth for every tailored resume, cover letter, and follow-up email the kit produces.

You can:
- **Edit it directly** in the text editor on the Candidate Profile tab.
- **Upload a `.md` file** to replace it entirely.
- **Generate it from your uploaded documents** using the profile generation prompt (see below).

Keep it current. The better the profile, the better every tailored document. Add new roles, certifications, and achievements as they happen.

## Document library

Upload your base resume, recommendation letters, certifications, and portfolio pieces once. Give each a short label and add a note about what it is and when it is relevant. These are stored once and reused every time you build a kit.

When Claude is connected in MCP mode, it reads these documents directly when tailoring application materials. For text-based files (`.md`, `.txt`) the content is returned inline; for PDF and Word files the metadata and your notes are returned, and Claude can reference them when generating documents.

**Asset kinds:**
- Base Resume
- Recommendation Letter
- Cover Letter Template
- Certification
- Portfolio
- Other

## Generating your profile from uploaded documents

If you have existing resume files uploaded, the **Profile generation prompt** section at the bottom of the Candidate Profile tab gives you a prompt you can use in Claude (MCP mode) to generate an updated profile from those files.

In MCP mode, Claude calls `get_candidate_assets()` to retrieve all your uploaded files, reads through them, and writes an updated profile that you can review and save back via `save_candidate_profile()`.

## Evaluating your documents

The **Evaluate documents prompt** (also on the Candidate Profile tab) asks Claude to review every file in your document library and return a structured assessment: what each document demonstrates, its strengths and weaknesses, and how well it supports your target roles. This is a quick way to find gaps in your application package before you start sending out kits.

## Good habits

- Whenever you get a new certification or a fresh recommendation letter, upload it to the document library right away so it is ready for the next kit.
- Review and update your master profile every few weeks, especially after completing a new project, earning a certification, or finishing a course.
- Keep salary targets current in your profile. The kit's fit-assessment step uses the salary floor from **Settings → Application Kit** to flag low-paying roles automatically.
