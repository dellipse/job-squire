# Connecting Job Sources

Open **Settings → Sources** to configure which job boards Job Squire searches automatically.
Some sources use public feeds and need no key at all — just enable them. Others use free API keys from the provider.

Every source has a **Test connection** button (verifies connectivity with one result) and a **Pull now** button (runs a full search immediately and saves new jobs to Job Squire). Secrets are encrypted at rest; leave a password field blank to keep the saved value.

---

## Indeed (via Claude connector)

Indeed blocks automated access through its public RSS feed but publishes an official [Claude connector](https://claude.ai/connectors). When the JobSquire MCP connector is active in Claude Pro, you can ask Claude to search Indeed as part of a supplemental search session and push results directly into your Job Squire instance via `add_jobs`.

No API key or Indeed partner account is needed for this path. You need:
- A Claude Pro subscription with the JobSquire MCP connector set up (see [Using Claude Pro](13-claude-pro.md)).
- The Indeed connector added in Claude's **Settings → Connectors**.

To run a manual Indeed search, open Claude and say something like:

> Search Indeed for [job title] jobs near [city, ST] within [X] miles, using my job-squire connector's get_search_targets for my exact criteria. Push any new matches with add_jobs.

The **Search jobs in Claude** button on **Settings → Search** generates a ready-to-use prompt that includes Indeed alongside ZipRecruiter, Dice, and Google Jobs.

Indeed is not available in the automated scheduler (Settings → Sources) — it runs through Claude only.

---

## Dice (no API key required)

Dice is a tech-focused board that uses a public RSS feed. No sign-up needed.

1. On the Sources tab, find **Dice**.
2. Tick **Use this source** and click Save.

## ZipRecruiter (free partner key)

ZipRecruiter requires a free partner API key.

1. Go to https://www.ziprecruiter.com/partner and apply for a partner key (free, approved quickly).
2. Paste the key into the **API Key** field, tick **Use this source**, and click Save.

## Google Jobs via SerpApi (free tier — broadest coverage)

SerpApi's Google Jobs engine aggregates postings from Indeed, LinkedIn, ZipRecruiter, Workday, Greenhouse, and hundreds of other boards in a single call, making it the highest-coverage single source available. The free tier includes **250 searches per month**.

1. Sign up at https://serpapi.com/users/sign_up (free).
2. Copy your API key from the SerpApi dashboard.
3. Paste the key into the **API Key** field.
4. Set **Max runs/day** to `1` to conserve credits (recommended starting point).
5. The **monthly query estimate** shown in the form updates as you adjust the limits — keep it under 250 to stay within the free tier.
6. Tick **Use this source** and click Save.

**How credits are used:** each page of 10 results costs 1 credit. With the default 25 results per query setting, each title costs ~3 credits per run. With 3 job titles and 1 run per day on weekdays, that is about 60 credits per month — well within the free tier.

## Jobicy (no API key required)

Jobicy uses a free public JSON API. No sign-up needed.

**Important limitation: Jobicy is remote-only.** Your location and radius settings have no effect — every result will show "Remote" as the location. Enable this source only if the candidate is open to remote work. It complements the other sources well for that segment.

1. On the Sources tab, find **Jobicy**.
2. Tick **Use this source** and click Save.

---

## Adzuna

Adzuna is a broad aggregator with good national coverage.

1. Go to https://developer.adzuna.com/ and sign up (free).
2. Create an application — they give you an **App ID** and an **App Key**.
3. Paste both into the Adzuna fields, tick **Use this source**, and click Save.

## Jooble

Jooble aggregates from many boards.

1. Go to https://jooble.org/api/about and request an API key (they email you one for free).
2. Paste it into the **API Key** field, tick **Use this source**, and click Save.

## The Muse (optional)

Good for operations and company-side roles. Works without a key, but a key raises rate limits.

1. Optional: get a key at https://www.themuse.com/developers/api/v2.
2. Paste it into the field (or leave blank), tick **Use this source**, and click Save.

## USAJOBS (optional — federal roles)

1. Go to https://developer.usajobs.gov/APIRequest/ and request a key. Provide an email; they send back an **Authorization Key**.
2. Enter the **registered email** you signed up with and the **Authorization Key**, tick **Use this source**, and click Save.

---

> LinkedIn and Monster block automated tools and are not available as sources. Indeed is not available in the automated scheduler but is accessible through its published Claude connector (see the Indeed section above). Google Jobs (via SerpApi) also provides indirect coverage of Indeed and LinkedIn through Google's aggregated job index. The eight automated sources plus Indeed via Claude give broad US coverage across all roles and experience levels. No automated feed catches every posting, so it is still worth browsing those sites directly from time to time. Add jobs you find manually with **+ Add job**, or use the [bookmarklet](08-bookmarklet.md) to capture them in one click.

---

## A note on metro-specific coverage

Several well-known regional job boards exist for major US metros but cannot be integrated as automated sources:

**Built In** (builtinchicago.org, builtinnyc.com, builtinboston.com, builtinla.com, builtinseattle.com) is the most prominent metro tech job network in the US — genuinely city-specific, strong in startup and mid-size tech companies. Their data feeds are employer-to-platform only (for posting jobs *into* Built In from an ATS), not public APIs for pulling listings out. Worth browsing directly for tech roles in those cities; use the bookmarklet to capture anything interesting.

**NYC city government jobs** are available via NYC Open Data (`data.cityofnewyork.us/resource/pda4-rgn4.json`, Socrata API). This is a public dataset with no key required — however, it covers only NYC municipal agency positions, not private-sector jobs. If the candidate is targeting public sector roles in New York City, this is a reliable source worth checking directly.

**State workforce boards** (CalJOBS, MassHire, Illinois workNet, etc.) are consumer portals with no standardized API. USAJOBS already covers federal roles. State boards are best accessed directly or through a career center.

For private-sector coverage in Chicago, New York, Boston, Los Angeles, and other major metros, the national sources — especially Google Jobs (SerpApi), ZipRecruiter, Adzuna, and Jooble — already surface regional postings well via the location and radius settings in **Settings → Search**.

---

## How searches run

The scheduler runs at 8 AM, 1 PM, and 5 PM on weekdays plus a weekend morning (in your search location's local time). Before each run fires, a small random delay is added so requests to job sources are spread out.

If a source returns an outage error (503), it is automatically parked in a cooldown for 4 hours and then resumed. This and any other errors show in the **History** tab on the Sources page.

Providers with a **Max runs/day** limit (currently Google Jobs/SerpApi) track their daily run count separately. Once the limit is reached, that provider is skipped for the rest of the day and resumes the following day.

A run that finds zero new jobs is normal once your sources have already pulled in what is currently posted. The Job Squire deduplicates across runs, so you will not see the same posting twice.
