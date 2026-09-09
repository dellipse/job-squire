# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for the additive schema migrations in app/__init__.py.

Migrations here are append-only ``ALTER TABLE ... ADD COLUMN`` statements that
run on every boot against the real user database. The safety properties that
matter:

  1. Running them on an older database adds the missing columns (upgrade path).
  2. Running them again is a no-op and never raises (idempotency).
  3. They never destroy existing rows.
  4. The one-time data backfill migrates legacy ``mode`` into the new
     api_enabled / mcp_enabled boolean flags.

There are no assertions about *how* a column is added, only that the observable
schema and data end up correct — so these tests stay valid if the migration list
is reordered or extended.
"""
import pytest
from flask import Flask
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import _run_migrations
from app.extensions import db

# A representative slice of columns that the migrations are responsible for
# adding. Not exhaustive, but spread across every table the migrations touch so
# a wrong table name or typo in any block is caught.
MIGRATED_COLUMNS = {
    "smtp_config": ["admin_email"],
    "ai_config": [
        "connector_name", "thinking_mode", "auto_triage_enabled", "triage_model",
        "auto_followup_enabled", "auto_weekly_review_enabled",
        "rejection_alert_threshold", "mcp_api_key_enc", "fallback_to_anthropic",
        "api_enabled", "mcp_enabled", "claude_buttons_enabled",
        "mcp_api_key_created_at", "mcp_api_key_last_used_at",
        "mcp_api_key_expires_at", "mcp_api_key_allow_network",
    ],
    "jobs": [
        "kit_output", "kit_generated_at", "ai_fit_score", "ai_fit_reason",
        "followup_draft", "kit_ats_gap",
    ],
    "interviews": ["prep_notes"],
    "search_runs": ["last_triage_at"],
    "users": ["jobs_default_sort", "jobs_default_status", "jobs_default_per_page"],
    "ai_provider_configs": ["use_for_triage", "use_for_analysis", "thinking_mode", "num_ctx"],
    "search_config": ["country"],
}


def _columns(table):
    rows = db.session.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return [r[1] for r in rows]


@pytest.fixture
def mdb():
    """Give each migration test its own throwaway, full-schema database.

    Earlier this dropped and rebuilt tables on the shared session-scoped
    ``app`` fixture's real database (see conftest.py), which made every other
    test in the suite order-dependent on migration tests never running
    in-between (test_smtp_settings.py, test_search_settings.py and
    test_ops.py all used to carry a defensive re-seed to route around it).

    Instead, build a private, function-scoped Flask app wired to an
    in-memory SQLite database and push its app context for the duration of
    the test. Flask-SQLAlchemy's models/metadata live on the shared ``db``
    object (imported above), not on any one Flask app -- only the engine and
    session are per-app -- so ``db.init_app()``ing this throwaway app and
    entering its context is enough to point every ``db.session`` call in
    ``_run_migrations()`` (and in the test bodies below) at this isolated
    database instead of the shared one, while still running the exact same
    real migration code path against a real schema.
    """
    migration_app = Flask(f"{__name__}-migration-app")
    migration_app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite://",  # in-memory; Flask-SQLAlchemy
                                               # applies StaticPool + check_same_thread=False automatically.
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        # _run_migrations()'s ProviderCredential integrity check reads this;
        # never actually decrypts anything since a fresh DB has no rows.
        SECRET_KEY="test-migration-secret-not-for-production-use",
    )
    db.init_app(migration_app)
    with migration_app.app_context():
        db.create_all()
        try:
            yield db
        finally:
            db.session.remove()
            db.engine.dispose()


def test_upgrade_adds_missing_columns(mdb):
    """Dropping migrated columns then running migrations restores every one."""
    # Simulate an old database: strip a spread of columns across tables.
    old_schema_drops = [
        ("jobs", "ai_fit_score"),
        ("jobs", "followup_draft"),
        ("jobs", "kit_ats_gap"),
        ("ai_config", "thinking_mode"),
        ("ai_config", "claude_buttons_enabled"),
        ("ai_config", "mcp_api_key_enc"),
        ("interviews", "prep_notes"),
        ("users", "jobs_default_sort"),
        ("search_runs", "last_triage_at"),
        ("smtp_config", "admin_email"),
        ("ai_provider_configs", "use_for_triage"),
        ("ai_provider_configs", "num_ctx"),
        ("search_config", "country"),
    ]
    for table, col in old_schema_drops:
        db.session.execute(text(f"ALTER TABLE {table} DROP COLUMN {col}"))
    db.session.commit()

    # Confirm the simulated old schema really is missing them.
    for table, col in old_schema_drops:
        assert col not in _columns(table), f"{table}.{col} should be gone pre-migration"

    _run_migrations()

    # Every dropped column is back.
    for table, col in old_schema_drops:
        assert col in _columns(table), f"{table}.{col} not restored by migration"


def test_all_expected_columns_present_after_migration(mdb):
    """After a clean create + migrate, every column the migrations own exists."""
    _run_migrations()
    for table, cols in MIGRATED_COLUMNS.items():
        present = _columns(table)
        for col in cols:
            assert col in present, f"expected {table}.{col} after migration"


def test_migrations_are_idempotent(mdb):
    """Running migrations repeatedly never raises and never duplicates a column."""
    _run_migrations()
    before = {t: _columns(t) for t in MIGRATED_COLUMNS}
    # Re-run twice more; the duplicate-column path must be swallowed cleanly.
    _run_migrations()
    _run_migrations()
    after = {t: _columns(t) for t in MIGRATED_COLUMNS}
    assert before == after, "column layout changed on repeated migration runs"
    # No column name should appear twice in any table.
    for table, cols in after.items():
        assert len(cols) == len(set(cols)), f"duplicate column in {table}: {cols}"


def test_migrations_preserve_existing_rows(mdb):
    """A row written before migration survives the ALTER statements intact."""
    db.session.execute(text(
        "INSERT INTO jobs (company, title, status) "
        "VALUES ('Acme', 'Engineer', 'Saved')"
    ))
    db.session.commit()

    _run_migrations()

    row = db.session.execute(
        text("SELECT company, title, status FROM jobs")
    ).fetchone()
    assert row is not None, "job row was lost during migration"
    assert row[0] == "Acme" and row[1] == "Engineer" and row[2] == "Saved"


def test_legacy_mode_backfill(mdb):
    """Legacy `mode` column is migrated into the api_enabled/mcp_enabled flags."""
    # api mode -> api_enabled should flip to 1
    db.session.execute(text(
        "INSERT INTO ai_config (id, mode, api_enabled, mcp_enabled) "
        "VALUES (1, 'api', 0, 0)"
    ))
    db.session.commit()

    _run_migrations()

    row = db.session.execute(
        text("SELECT api_enabled, mcp_enabled FROM ai_config WHERE id=1")
    ).fetchone()
    assert row[0] == 1, "mode='api' should backfill api_enabled=1"
    assert row[1] == 0, "mcp_enabled should stay 0 for an api-mode config"


def test_legacy_mcp_mode_backfill(mdb):
    """mode='mcp' backfills mcp_enabled=1 AND bootstraps the Claude buttons.

    Regression guard: the claude_buttons_enabled bootstrap is guarded by the
    mcp_enabled=0 first-boot sentinel, so it must run before the mcp_enabled flip
    in _run_migrations(). If those statements are ever reordered so the flip runs
    first, the bootstrap goes dead and this test fails.
    """
    db.session.execute(text(
        "INSERT INTO ai_config (id, mode, api_enabled, mcp_enabled, claude_buttons_enabled) "
        "VALUES (1, 'mcp', 0, 0, 0)"
    ))
    db.session.commit()

    _run_migrations()

    row = db.session.execute(
        text("SELECT mcp_enabled, claude_buttons_enabled FROM ai_config WHERE id=1")
    ).fetchone()
    assert row[0] == 1, "mode='mcp' should backfill mcp_enabled=1"
    assert row[1] == 1, "mode='mcp' first boot should enable the Claude buttons"


def test_search_config_country_backfills_to_us(mdb):
    """Existing installs (pre-country-column) default to 'US', preserving current
    behavior — strict City/ST validation and the Adzuna /us/ endpoint."""
    db.session.execute(text(
        "ALTER TABLE search_config DROP COLUMN country"
    ))
    db.session.execute(text(
        "INSERT INTO search_config (id, location) VALUES (1, 'Boise, ID')"
    ))
    db.session.commit()

    _run_migrations()

    row = db.session.execute(
        text("SELECT country FROM search_config WHERE id=1")
    ).fetchone()
    assert row[0] == "US"


# ---------------------------------------------------------------------------
# REL-03: uq_jobs_source_external_id must land safely on a database that
# already has duplicate (source, external_id) rows -- the exact situation any
# real pre-fix install could be in, since ingest_jobs() was read-then-insert
# with no DB-level guard until this migration.
# ---------------------------------------------------------------------------

@pytest.fixture
def mdb_bare_jobs():
    """A throwaway DB whose ``jobs`` table is built by hand in the *pre-fix*
    shape (no uq_jobs_source_external_id, external_id defaulting to '' rather
    than NULL) instead of ``db.create_all()``.

    The plain ``mdb`` fixture above builds every table from the *current*
    models, which already bakes uq_jobs_source_external_id into the jobs
    table's CREATE TABLE statement (as a SQLite ``sqlite_autoindex``, which
    can't even be dropped after the fact) -- so it can't hold the duplicate
    rows this test needs to insert. This fixture isolates the migration's
    dedupe/backfill logic from that already-fixed schema, the same way
    ``test_upgrade_adds_missing_columns`` isolates a dropped column.
    """
    migration_app = Flask(f"{__name__}-bare-jobs-app")
    migration_app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite://",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="test-migration-secret-not-for-production-use",
    )
    db.init_app(migration_app)
    with migration_app.app_context():
        db.session.execute(text(
            "CREATE TABLE jobs ("
            "id INTEGER PRIMARY KEY, company VARCHAR(160) NOT NULL, "
            "title VARCHAR(160) NOT NULL, source VARCHAR(80) DEFAULT '', "
            "external_id VARCHAR(255) DEFAULT '', status VARCHAR(40) DEFAULT 'Applied')"
        ))
        db.session.commit()
        try:
            yield db
        finally:
            db.session.remove()
            db.engine.dispose()


def test_migration_dedupes_preexisting_duplicate_external_id(mdb_bare_jobs):
    """A pre-fix DB with a genuine duplicate (source, external_id) pair, plus
    the old '' external_id sentinel, must come out of the migration with:
    no rows deleted, exactly one row per (source, external_id) group keeping
    its external_id (the rest NULLed, not lost), '' normalized to NULL, and
    the new unique index actually enforcing going forward."""
    db.session.execute(text(
        "INSERT INTO jobs (company, title, source, external_id, status) VALUES "
        "('DupCoOld', 'Engineer', 'jooble', 'dup-A', 'Saved'),"
        "('DupCoNew', 'Different Title', 'jooble', 'dup-A', 'Applied'),"
        "('SoloCo', 'Analyst', 'jooble', 'solo-1', 'Saved'),"
        "('NoIdCoA', 'QA One', 'referral', '', 'Saved'),"
        "('NoIdCoB', 'QA Two', 'referral', '', 'Saved')"
    ))
    db.session.commit()

    assert db.session.execute(text("SELECT COUNT(*) FROM jobs")).scalar() == 5

    _run_migrations()

    # No data destroyed.
    assert db.session.execute(text("SELECT COUNT(*) FROM jobs")).scalar() == 5

    # Only the oldest (lowest id) row of the duplicate pair keeps external_id.
    dup_rows = db.session.execute(text(
        "SELECT company, external_id FROM jobs "
        "WHERE source='jooble' AND external_id='dup-A' ORDER BY id"
    )).fetchall()
    assert len(dup_rows) == 1
    assert dup_rows[0][0] == "DupCoOld"

    loser_external_id = db.session.execute(text(
        "SELECT external_id FROM jobs WHERE company='DupCoNew'"
    )).scalar()
    assert loser_external_id is None, "losing duplicate must be NULLed, not deleted"

    # The non-duplicate row is untouched.
    solo_external_id = db.session.execute(text(
        "SELECT external_id FROM jobs WHERE company='SoloCo'"
    )).scalar()
    assert solo_external_id == "solo-1"

    # The old '' sentinel is gone; no row has an empty-string external_id.
    assert db.session.execute(
        text("SELECT COUNT(*) FROM jobs WHERE external_id = ''")
    ).scalar() == 0

    # Both no-id 'referral' jobs survive as distinct rows (NULL != NULL).
    no_id_count = db.session.execute(text(
        "SELECT COUNT(*) FROM jobs WHERE source='referral' AND external_id IS NULL"
    )).scalar()
    assert no_id_count == 2

    # The index now exists and is a real constraint going forward.
    idx = db.session.execute(text(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='uq_jobs_source_external_id'"
    )).fetchone()
    assert idx is not None

    with pytest.raises(IntegrityError):
        db.session.execute(text(
            "INSERT INTO jobs (company, title, source, external_id, status) "
            "VALUES ('ShouldFail', 'X', 'jooble', 'solo-1', 'Saved')"
        ))
        db.session.commit()
    db.session.rollback()


def test_migration_dedupe_is_idempotent_on_rerun(mdb_bare_jobs):
    """Running the dedupe/index migration again after it already succeeded must
    not raise and must not touch the now-correct data further."""
    db.session.execute(text(
        "INSERT INTO jobs (company, title, source, external_id, status) VALUES "
        "('DupCoOld', 'Engineer', 'jooble', 'dup-B', 'Saved'),"
        "('DupCoNew', 'Different Title', 'jooble', 'dup-B', 'Applied')"
    ))
    db.session.commit()

    _run_migrations()
    first_pass = db.session.execute(text(
        "SELECT id, external_id FROM jobs ORDER BY id"
    )).fetchall()

    _run_migrations()
    _run_migrations()
    second_pass = db.session.execute(text(
        "SELECT id, external_id FROM jobs ORDER BY id"
    )).fetchall()

    assert first_pass == second_pass
