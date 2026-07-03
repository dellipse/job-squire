# Quick Start: 15-Minute Setup

Do these steps once, in order. After this Job Squire is working.

## Step 1 — Sign in

Use the credentials your admin provided. If you have not received them, ask your admin.

## Step 2 — Connect job sources

Open **Settings** (top menu) → **Sources** tab.

Connect at least **Adzuna** and **Jooble**. Each needs a free API key from that provider.

**Adzuna:**
1. Go to https://developer.adzuna.com/ and sign up (free).
2. Create an application — they give you an **App ID** and an **App Key**.
3. Paste both into the Adzuna fields, tick **Use this source**, and click Save.

**Jooble:**
1. Go to https://jooble.org/api/about and request an API key (they email you one).
2. Paste it into the Jooble **API Key** field, tick **Use this source**, and click Save.

You can add ZipRecruiter, Google Jobs (SerpApi), Dice (no key), Jobicy (no key), USAJOBS, and The Muse later. See [Connecting Job Sources](03-job-sources.md) for all eight providers.

## Step 3 — Set your search targets

Switch to the **Search** tab in Settings. Fill in:

- **Titles** — one target job title per line (e.g., "Operations Manager", "Supply Chain Coordinator").
- **Location** — your city and state in `City, ST` format, e.g., `Columbus, OH`.
- Adjust radius and max posting age if needed.

## Step 4 — Set up email digests

Switch to the **Email** tab. Confirm your email address is in the **Send to** field.

If the SMTP settings are blank, ask your admin to fill them in. The Job Squire sends you a digest whenever it finds new matching roles.

## Step 5 — Run your first search

Click **Run search now** in the top-right of the Settings page. Switch to the **History** tab and watch the results come in. New postings appear in your Jobs list under the **Saved** status.

---

After these five steps, the search runs automatically — 8 AM, 1 PM, and 5 PM on weekdays, plus a weekend morning — emailing you only when it finds something new.

**Next:** [How the Job Squire Is Organized](02-navigation.md)
