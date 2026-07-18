# Copyright (C) 2026 D. Brandmeyer
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Search orchestration: query providers, dedupe, ingest, notify."""
import fcntl
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import current_app

from .crypto import decrypt
from .db_utils import commit
from .extensions import db
from .models import Job, ProviderCredential, SearchConfig, SearchRun, SmtpConfig, User
from .notify import build_digest, build_error_report, send_email
from .providers import PROVIDERS, REMOTE_ONLY_PROVIDERS, search_provider

log = logging.getLogger(__name__)

# Per-provider cooldown: after a 503 outage, skip that provider for this many
# hours so it doesn't burn every run until the outage clears.
_COOLDOWN_HOURS = float(os.environ.get("PROVIDER_COOLDOWN_HOURS", "4"))
_COOLDOWN_FILE = Path(os.environ.get("DATA_DIR", "/data")) / "provider_cooldowns.json"
# Per-provider daily run counts (for providers with a max_runs_per_day limit).
_DAILY_RUNS_FILE = Path(os.environ.get("DATA_DIR", "/data")) / "provider_daily_runs.json"


def _load_cooldowns() -> dict:
    try:
        if _COOLDOWN_FILE.exists():
            return json.loads(_COOLDOWN_FILE.read_text())
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_cooldowns(cooldowns: dict) -> None:
    try:
        _COOLDOWN_FILE.write_text(json.dumps(cooldowns))
    except Exception:  # noqa: BLE001
        log.warning("could not write cooldown file %s", _COOLDOWN_FILE)


def _in_cooldown(cooldowns: dict, provider: str) -> bool:
    ts = cooldowns.get(provider)
    if not ts:
        return False
    try:
        return datetime.now(timezone.utc).replace(tzinfo=None) < datetime.fromisoformat(ts)
    except Exception:  # noqa: BLE001
        return False


def _set_cooldown(cooldowns: dict, provider: str) -> None:
    until = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=_COOLDOWN_HOURS)).isoformat(timespec="seconds")
    cooldowns[provider] = until
    log.info("%s: cooldown set until %s UTC", provider, until)


def _load_daily_runs() -> dict:
    """Load per-provider daily run counts from disk."""
    try:
        if _DAILY_RUNS_FILE.exists():
            return json.loads(_DAILY_RUNS_FILE.read_text())
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_daily_runs(counts: dict) -> None:
    try:
        _DAILY_RUNS_FILE.write_text(json.dumps(counts))
    except Exception:  # noqa: BLE001
        log.warning("could not write daily runs file %s", _DAILY_RUNS_FILE)


def _provider_runs_today(counts: dict, provider: str) -> int:
    """Return how many times this provider has run today (UTC date)."""
    entry = counts.get(provider) or {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if entry.get("date") != today:
        return 0
    return int(entry.get("count", 0))


def _increment_provider_runs(counts: dict, provider: str) -> None:
    """Increment today's run count for this provider."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = counts.get(provider) or {}
    if entry.get("date") != today:
        counts[provider] = {"date": today, "count": 1}
    else:
        counts[provider] = {"date": today, "count": int(entry.get("count", 0)) + 1}


def _norm(s):
    return (s or "").strip().lower()


def ingest_jobs(items, created_by="auto-search", default_status="Saved"):
    """Insert normalized job dicts, skipping duplicates. Returns (created_jobs, skipped)."""
    created, skipped = [], 0
    seen_keys = set()

    for it in items:
        title = (it.get("title") or "").strip()
        company = (it.get("company") or "").strip()
        if not title or not company:
            skipped += 1
            continue
        source = (it.get("source") or "").strip()
        ext = (str(it.get("external_id")) if it.get("external_id") else "").strip()

        # In-batch dedupe key.
        if ext:
            batch_key = ("ext", source, ext)
        else:
            batch_key = ("ct", _norm(company), _norm(title), _norm(it.get("location")))
        if batch_key in seen_keys:
            skipped += 1
            continue
        seen_keys.add(batch_key)

        # DB dedupe.
        exists = None
        if ext:
            exists = Job.query.filter_by(source=source, external_id=ext).first()
        if not exists:
            exists = (
                Job.query.filter(db.func.lower(Job.company) == _norm(company))
                .filter(db.func.lower(Job.title) == _norm(title))
                .first()
            )
        if exists:
            skipped += 1
            continue

        notes = (it.get("description") or "").strip()
        meta = []
        if it.get("date_posted"):
            meta.append(f"Posted: {it['date_posted']}")
        if source:
            meta.append(f"Found via: {source}")
        if meta:
            notes = (notes + "\n\n" + " · ".join(meta)).strip()

        job = Job(
            company=company,
            title=title,
            location=(it.get("location") or "").strip(),
            work_mode="Unknown",
            source=source,
            external_id=ext,
            url=(it.get("url") or "").strip(),
            salary=(it.get("salary") or "").strip(),
            status=default_status,
            notes=notes,
            created_by=created_by,
        )
        db.session.add(job)
        created.append(job)

    commit()
    return created, skipped


def _decrypt_creds(secret_key, blob):
    raw = decrypt(secret_key, blob)
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def run_search(trigger="manual"):
    """Execute a full search across enabled providers. Must run in app context.

    Uses an exclusive flock on a run-lock file so concurrent scheduler workers
    (or a manual run that overlaps a scheduled one) don't execute in parallel and
    hammer provider APIs with duplicate requests.
    """
    data_dir = current_app.config.get("DATA_DIR", "/data")
    run_lock_path = os.path.join(data_dir, ".search_run.lock")
    run_lock_file = open(run_lock_path, "w")  # noqa: WPS515 — kept open for duration
    try:
        # LOCK_NB = non-blocking; raises BlockingIOError if already locked.
        fcntl.flock(run_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("run_search: another run is already in progress — skipping (%s)", trigger)
        run_lock_file.close()
        return None

    try:
        return _run_search_locked(trigger)
    finally:
        fcntl.flock(run_lock_file, fcntl.LOCK_UN)
        run_lock_file.close()


def _mark_progress(run, message: str, *, found_so_far: int | None = None) -> None:
    """Write a live one-line progress update to a "running" SearchRun row.

    Committed immediately (not batched with the rest of the run) so every
    poll of the Getting Started / Search History pages while a run is in
    flight shows real movement instead of a static "still running" message
    for the whole run. This matters because a run can legitimately take much
    longer than it looks like it should: THROTTLE_SECONDS in providers.py
    defaults to 60-120s *between every title* on a given provider, and
    providers run one at a time, so a handful of titles across a few boards
    can genuinely take tens of minutes with nothing to show for it in the
    meantime -- which is what made this look "stuck" even though it wasn't.
    """
    run.detail = message[:1000]
    if found_so_far is not None:
        run.found = found_so_far
    commit()


def _run_search_locked(trigger="manual"):
    """Inner search logic — called only when the run lock is held."""
    secret_key = current_app.config["SECRET_KEY"]
    cfg_row = db.session.get(SearchConfig, 1)
    if not cfg_row:
        return None
    # `enabled` is the "Automated search (3x/day on weekdays)" toggle -- it
    # governs the scheduler only. A manual run (the "Run first search now"
    # button and Settings -> Run, both trigger="manual") must execute even when
    # automated search is off; otherwise run_search() returns here without ever
    # creating a "running" SearchRun row, and the Getting Started page polls
    # forever for a run that never appears (no stopwatch, no update, no finish).
    if trigger == "scheduled" and not cfg_row.enabled:
        return None
    titles = cfg_row.title_list
    if not titles:
        return None

    cfg = {
        "location": cfg_row.location or "",
        "country": cfg_row.country or "US",
        "radius_miles": cfg_row.radius_miles or 40,
        "min_salary": cfg_row.min_salary,
        "max_age_days": cfg_row.max_age_days or 14,
        "results_per_query": cfg_row.results_per_query or 25,
        # NULL (pre-migration rows) must behave like the default: True.
        "include_remote": cfg_row.include_remote is not False,
    }

    enabled = ProviderCredential.query.filter_by(enabled=True).all()
    run = SearchRun(trigger=trigger, status="running",
                    providers=",".join(p.provider for p in enabled))
    db.session.add(run)
    commit()

    # Everything below runs with a "running" SearchRun row already committed.
    # Anything that raises here — most likely ingest_jobs()'s commit() hitting
    # a SQLite "database is locked"/"disk I/O error" it can't retry its way
    # out of (see db_utils.py's docstring re: the shared 3-container /data
    # volume) — used to propagate straight out of this daemon thread (see
    # main.py's settings_run/_bg_search) and get swallowed silently, leaving
    # the row stuck at status="running" forever with the Getting Started page
    # polling it indefinitely. Wrap the whole body so any failure still gets
    # a final status instead of stranding the row.
    try:
        cooldowns = _load_cooldowns()
        cooldowns_dirty = False
        daily_runs = _load_daily_runs()
        daily_runs_dirty = False

        all_items, notes = [], []
        total = len(enabled)
        if total:
            _mark_progress(run, f"Starting search across {total} job board(s)…")
        for idx, pc in enumerate(enabled, start=1):
            if pc.provider not in PROVIDERS:
                continue
            if pc.provider in REMOTE_ONLY_PROVIDERS and not cfg.get("include_remote", True):
                note = (f"{pc.provider}: remote-only board skipped (remote jobs are "
                        "turned off in search settings)")
                notes.append(note)
                _mark_progress(run, f"Skipping {pc.provider} ({idx}/{total}): remote jobs "
                                     f"are off — {len(all_items)} found so far.")
                continue
            if _in_cooldown(cooldowns, pc.provider):
                until = cooldowns[pc.provider]
                notes.append(f"{pc.provider}: in cooldown until {until} UTC — skipping")
                log.info("%s: skipped (cooldown until %s UTC)", pc.provider, until)
                _mark_progress(run, f"Skipping {pc.provider} ({idx}/{total}): in cooldown — "
                                     f"{len(all_items)} found so far.")
                continue
            creds = _decrypt_creds(secret_key, pc.secret_blob)

            # Per-provider daily run limit (optional; stored in provider creds).
            max_runs = int(creds.get("max_runs_per_day") or 0)
            if max_runs > 0:
                runs_today = _provider_runs_today(daily_runs, pc.provider)
                if runs_today >= max_runs:
                    notes.append(
                        f"{pc.provider}: daily run limit ({max_runs}/day) reached — skipping"
                    )
                    log.info("%s: skipped (daily run limit %d reached, %d runs today)",
                             pc.provider, max_runs, runs_today)
                    _mark_progress(run, f"Skipping {pc.provider} ({idx}/{total}): daily limit "
                                         f"reached — {len(all_items)} found so far.")
                    continue

            # Per-provider title limit (optional; trims the title list for this run).
            max_titles = int(creds.get("max_titles_per_run") or 0)
            run_titles = titles[:max_titles] if max_titles > 0 else titles

            # This is the slow part: search_provider() calls one title at a
            # time with a THROTTLE_SECONDS pause (default 60-120s) between
            # each, so this single line can legitimately take many minutes
            # for a provider with several titles configured.
            _mark_progress(run, f"Searching {pc.provider} ({idx}/{total}) — "
                                 f"{len(run_titles)} title(s) — {len(all_items)} found so far…")
            results, err = search_provider(pc.provider, creds, run_titles, cfg)

            # Count this as a run regardless of outcome (API credits were consumed).
            if max_runs > 0:
                _increment_provider_runs(daily_runs, pc.provider)
                daily_runs_dirty = True

            if err:
                notes.append(err)
                if "503" in err or "service unavailable" in err.lower():
                    _set_cooldown(cooldowns, pc.provider)
                    cooldowns_dirty = True
            all_items.extend(results)
            _mark_progress(
                run,
                f"Finished {pc.provider} ({idx}/{total}) — {len(all_items)} found so far."
                if not err else
                f"{pc.provider} ({idx}/{total}) hit an error, continuing — "
                f"{len(all_items)} found so far.",
                found_so_far=len(all_items),
            )

        if cooldowns_dirty:
            _save_cooldowns(cooldowns)
        if daily_runs_dirty:
            _save_daily_runs(daily_runs)

        created, skipped = ingest_jobs(all_items, created_by="auto-search")

        emailed = False
        if created:
            try:
                emailed = _maybe_email(secret_key, created)
            except Exception as e:  # noqa: BLE001
                notes.append(f"digest email failed: {e.__class__.__name__}")

        if notes:
            try:
                _maybe_error_email(secret_key, notes, trigger=trigger)
            except Exception as e:  # noqa: BLE001
                log.warning("error notification email failed: %s", e)

        run.finished_at = datetime.now(timezone.utc)
        run.found = len(all_items)
        run.created = len(created)
        run.skipped = skipped
        run.emailed = emailed
        run.status = "error" if (notes and not all_items) else "ok"
        run.detail = " | ".join(notes)[:1000]
        commit()
        return run
    except Exception as e:  # noqa: BLE001 — never leave a SearchRun stuck at "running"
        log.exception("run_search: unhandled error mid-run (trigger=%s)", trigger)
        try:
            db.session.rollback()
            run.finished_at = datetime.now(timezone.utc)
            run.status = "error"
            run.detail = f"internal error: {e.__class__.__name__}: {e}"[:1000]
            commit()
        except Exception:  # noqa: BLE001 — DB itself is what's broken; nothing more to do
            log.exception("run_search: also failed to record the error on the SearchRun row")
        return None


def _smtp_dict(smtp_row, secret_key):
    return {
        "host": smtp_row.host,
        "port": smtp_row.port,
        "use_tls": smtp_row.use_tls,
        "username": smtp_row.username,
        "password": decrypt(secret_key, smtp_row.password_enc),
        "from_addr": smtp_row.from_addr,
        "to_addr": smtp_row.to_addr,
    }


def _maybe_email(secret_key, created_jobs):
    smtp_row = db.session.get(SmtpConfig, 1)
    if not smtp_row or not smtp_row.enabled or not smtp_row.host or not smtp_row.to_addr:
        return False
    base_url = os.environ.get("PUBLIC_URL", "")
    candidate_user = User.query.filter_by(role="user").first()
    recipient_name = (candidate_user.display_name or candidate_user.username) if candidate_user else "you"
    subject, text, html = build_digest(created_jobs, base_url=base_url or None, recipient_name=recipient_name)
    smtp = _smtp_dict(smtp_row, secret_key)
    if not smtp["password"] and smtp_row.password_enc:
        log.warning("SMTP password could not be decrypted — SECRET_KEY may have changed; re-enter credentials in Settings.")
        return False
    send_email(smtp, subject, text, html)
    return True


def _maybe_error_email(secret_key, errors, trigger="scheduled"):
    """Send an error alert to the admin, and to the job-seeker if their address differs."""
    smtp_row = db.session.get(SmtpConfig, 1)
    if not smtp_row or not smtp_row.enabled or not smtp_row.host:
        return
    admin = (smtp_row.admin_email or "").strip()
    user = (smtp_row.to_addr or "").strip()
    if not admin and not user:
        return

    smtp = _smtp_dict(smtp_row, secret_key)
    if not smtp["password"] and smtp_row.password_enc:
        log.warning("SMTP password could not be decrypted — SECRET_KEY may have changed; re-enter credentials in Settings.")
        return

    # Primary recipient is admin; if user is set and different, CC them too.
    if admin:
        smtp["to_addr"] = admin
        extra = [user] if user and user != admin else []
    else:
        # No admin address configured — fall back to user-only.
        smtp["to_addr"] = user
        extra = []

    base_url = os.environ.get("PUBLIC_URL", "")
    subject, text, html = build_error_report(errors, trigger=trigger, base_url=base_url or None)
    send_email(smtp, subject, text, html, extra_to=extra)
