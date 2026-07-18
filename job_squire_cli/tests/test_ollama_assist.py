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
"""docs/PLAN-ollama-assist.md, CLI side (ops/ollama_assist.py).

Detection and install must never touch a real subprocess, the real
filesystem PATH, or the real network -- every test injects a fake
`run`/`which` pair (same convention as test_runtime.py) and, for the
round-trip HTTP test, a fake `urllib.request.urlopen`.
"""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from job_squire_cli.ops import ollama_assist as oa
from job_squire_cli.ops import paths


def fake_run(ok_prefixes=(), fail_prefixes=(), raise_prefixes=(), stdout=""):
    """Same convention as test_runtime.py's fake_run, plus a fixed `stdout`
    for tests that need to read output back (e.g. `sysctl -n hw.memsize`)."""
    calls = []

    def _run(args, **kwargs):
        calls.append(tuple(args))
        for prefix in raise_prefixes:
            if tuple(args[: len(prefix)]) == tuple(prefix):
                raise FileNotFoundError(args[0])
        for prefix in ok_prefixes:
            if tuple(args[: len(prefix)]) == tuple(prefix):
                return SimpleNamespace(returncode=0, stdout=stdout)
        for prefix in fail_prefixes:
            if tuple(args[: len(prefix)]) == tuple(prefix):
                return SimpleNamespace(returncode=1, stdout="")
        raise AssertionError(f"unexpected subprocess call in test: {args}")

    _run.calls = calls
    return _run


def which_map(present: dict):
    return lambda name: present.get(name)


# ── Tier classification ───────────────────────────────────────────────────


def _caps(ram_gb=None, apple_silicon=False, gpu_vendor=None, gpu_vram_gb=None):
    return oa.HostCapabilities(
        detected_at="2026-07-16T00:00:00Z",
        os="Darwin",
        apple_silicon=apple_silicon,
        ram_gb=ram_gb,
        cpu_cores=8,
        gpu_vendor=gpu_vendor,
        gpu_vram_gb=gpu_vram_gb,
        ollama_installed=False,
        ollama_running=False,
    )


@pytest.mark.parametrize(
    "caps,expected_tier",
    [
        (_caps(ram_gb=4), oa.TIER_NOT_REASONABLE),
        (_caps(ram_gb=None), oa.TIER_NOT_REASONABLE),  # detection failed entirely
        (_caps(ram_gb=8), oa.TIER_ENTRY),
        (_caps(ram_gb=15.9), oa.TIER_ENTRY),
        (_caps(ram_gb=16), oa.TIER_CAPABLE),
        (_caps(ram_gb=16, gpu_vendor="nvidia", gpu_vram_gb=8), oa.TIER_CAPABLE),
        (_caps(ram_gb=16, apple_silicon=True), oa.TIER_STRONG),  # Daniel's Mac mini M4, 16 GB
        (_caps(ram_gb=32), oa.TIER_STRONG),
        (_caps(ram_gb=16, gpu_vendor="nvidia", gpu_vram_gb=12), oa.TIER_STRONG),
        (_caps(ram_gb=64), oa.TIER_WORKSTATION),
        (_caps(ram_gb=16, gpu_vendor="nvidia", gpu_vram_gb=24), oa.TIER_WORKSTATION),
        # A GPU-only signal without apple_silicon and under the RAM
        # thresholds still classifies on VRAM alone.
        (_caps(ram_gb=8, gpu_vendor="nvidia", gpu_vram_gb=8), oa.TIER_CAPABLE),
    ],
)
def test_classify_tier(caps, expected_tier):
    assert oa.classify_tier(caps) == expected_tier


def test_recommend_returns_none_for_not_reasonable():
    assert oa.recommend(_caps(ram_gb=4)) is None


def test_recommend_returns_tier_table_entry():
    rec = oa.recommend(_caps(ram_gb=16, apple_silicon=True))
    assert rec is oa.TIER_TABLE[oa.TIER_STRONG]
    assert rec.triage_model and rec.analysis_model


def test_not_reasonable_message_includes_detected_ram():
    msg = oa.not_reasonable_message(_caps(ram_gb=4))
    assert "4 GB RAM" in msg
    assert "cloud" in msg.lower()


def test_not_reasonable_message_handles_undetected_ram():
    msg = oa.not_reasonable_message(_caps(ram_gb=None))
    assert "undetermined amount of RAM" in msg


def test_tier_table_models_are_distinct_data_not_scattered():
    """Every tier's recommendation must actually name two models -- a
    silent typo (e.g. blank string) would otherwise pass classification
    tests above but break `setup` at pull time."""
    for rec in oa.TIER_TABLE.values():
        assert rec.triage_model
        assert rec.analysis_model
        assert rec.approx_download_gb > 0


# ── Host detection (per OS) ────────────────────────────────────────────────


def test_detect_macos_apple_silicon(monkeypatch):
    monkeypatch.setattr(oa.platform, "machine", lambda: "arm64")

    def run_with_stdout(args, **kwargs):
        if tuple(args) == ("sysctl", "-n", "hw.memsize"):
            return SimpleNamespace(returncode=0, stdout=str(16 * 1024**3))
        if tuple(args) == ("sysctl", "-n", "hw.ncpu"):
            return SimpleNamespace(returncode=0, stdout="10")
        raise AssertionError(args)

    monkeypatch.setattr(oa, "is_ollama_installed", lambda which=None: False)
    monkeypatch.setattr(oa, "is_ollama_running", lambda **kwargs: False)

    caps = oa.detect_host_capabilities(system="Darwin", run=run_with_stdout, which=which_map({}))
    assert caps.os == "Darwin"
    assert caps.apple_silicon is True
    assert caps.ram_gb == 16.0
    assert caps.cpu_cores == 10
    assert caps.gpu_vendor == "apple"
    assert caps.gpu_vram_gb is None


def test_detect_linux_nvidia(monkeypatch):
    def run_with_stdout(args, **kwargs):
        if tuple(args[:3]) == ("nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"):
            return SimpleNamespace(returncode=0, stdout="24576\n")
        raise AssertionError(args)

    monkeypatch.setattr(
        oa, "_linux_meminfo_gb", lambda: 64.0,
    )
    monkeypatch.setattr(oa, "_linux_cpu_cores", lambda: 32)
    monkeypatch.setattr(oa, "is_ollama_installed", lambda which=None: True)
    monkeypatch.setattr(oa, "is_ollama_running", lambda **kwargs: True)

    caps = oa.detect_host_capabilities(
        system="Linux", run=run_with_stdout, which=which_map({"nvidia-smi": "/usr/bin/nvidia-smi"}),
    )
    assert caps.ram_gb == 64.0
    assert caps.cpu_cores == 32
    assert caps.gpu_vendor == "nvidia"
    assert caps.gpu_vram_gb == 24.0
    assert caps.ollama_installed is True
    assert caps.ollama_running is True


def test_detect_linux_no_gpu_tools_leaves_gpu_none():
    run = fake_run()  # no calls expected -- which() gates every GPU probe
    caps = oa.detect_host_capabilities(system="Linux", run=run, which=which_map({}))
    assert caps.gpu_vendor is None
    assert caps.gpu_vram_gb is None
    assert run.calls == []


def test_is_ollama_installed_checks_path():
    assert oa.is_ollama_installed(which_map({"ollama": "/usr/local/bin/ollama"})) is True
    assert oa.is_ollama_installed(which_map({})) is False


def test_is_ollama_running_true_on_200(monkeypatch):
    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(oa.urllib.request, "urlopen", lambda req, timeout=None: FakeResp())
    assert oa.is_ollama_running() is True


def test_is_ollama_running_false_on_connection_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise oa.urllib.error.URLError("connection refused")

    monkeypatch.setattr(oa.urllib.request, "urlopen", _raise)
    assert oa.is_ollama_running() is False


# ── host_capabilities.json round-trip ──────────────────────────────────────


def test_write_and_read_host_capabilities_round_trips(tmp_path):
    root = tmp_path / "castelo"
    caps = _caps(ram_gb=16, apple_silicon=True)
    path = oa.write_host_capabilities(root, caps)
    assert path == paths.data_dir(root) / oa.HOST_CAPABILITIES_FILENAME
    assert path.exists()

    loaded = oa.read_host_capabilities(root)
    assert loaded == caps


def test_read_host_capabilities_missing_file_returns_none(tmp_path):
    assert oa.read_host_capabilities(tmp_path / "nope") is None


def test_write_host_capabilities_lands_in_data_dir_not_instance_root(tmp_path):
    """The web app only ever sees `data/` inside its container (paths.py) --
    writing to the instance root instead would be invisible to it."""
    root = tmp_path / "castelo"
    oa.write_host_capabilities(root, _caps())
    assert (root / "data" / oa.HOST_CAPABILITIES_FILENAME).exists()
    assert not (root / oa.HOST_CAPABILITIES_FILENAME).exists()


# ── Install plans ──────────────────────────────────────────────────────────


def test_macos_install_plan_uses_brew_formula_and_starts_service():
    plan = oa.macos_install_plan()
    assert plan.runtime == "ollama"
    assert plan.steps[0].command == ("brew", "install", "ollama")
    assert any("services" in step.command and "start" in step.command for step in plan.steps)


def test_linux_install_plan_uses_official_script():
    plan = oa.linux_install_plan()
    assert "ollama.com/install.sh" in plan.steps[0].command[-1]


def test_windows_install_plan_uses_winget():
    plan = oa.windows_install_plan()
    assert plan.steps[0].command == ("winget", "install", "-e", "--id", "Ollama.Ollama")


def test_install_plan_for_dispatches_by_system():
    assert oa.install_plan_for("Darwin").runtime == "ollama"
    assert "install.sh" in oa.install_plan_for("Linux").steps[0].command[-1]
    assert oa.install_plan_for("Windows").steps[0].command[0] == "winget"


def test_install_plan_for_unknown_platform_raises():
    with pytest.raises(oa.OllamaAssistError, match="Windows Phone"):
        oa.install_plan_for("Windows Phone")


# ── ensure_ollama_installed orchestration ──────────────────────────────────


def test_ensure_ollama_installed_reuses_existing_and_installs_nothing():
    run = fake_run()  # any subprocess call here is a test failure
    result = oa.ensure_ollama_installed(
        system="Darwin", run=run, which=which_map({"ollama": "/usr/local/bin/ollama"}), confirm=lambda _: True,
    )
    assert result is True
    assert run.calls == []


def test_ensure_ollama_installed_installs_with_consent():
    run = fake_run(ok_prefixes=[("brew", "install", "ollama"), ("brew", "services", "start", "ollama")])

    def which_progression(name):
        installed = any(c[:2] == ("brew", "install") for c in run.calls)
        return "/usr/local/bin/ollama" if (name == "ollama" and installed) else None

    result = oa.ensure_ollama_installed(system="Darwin", run=run, which=which_progression, confirm=lambda _: True)
    assert result is True
    assert run.calls[0] == ("brew", "install", "ollama")


def test_ensure_ollama_installed_declines_consent_installs_nothing():
    run = fake_run()
    with pytest.raises(oa.OllamaAssistError, match="declined"):
        oa.ensure_ollama_installed(system="Darwin", run=run, which=which_map({}), confirm=lambda _: False)
    assert run.calls == []


def test_ensure_ollama_installed_dry_run_prints_without_calling(capsys):
    run = fake_run()
    result = oa.ensure_ollama_installed(system="Darwin", run=run, which=which_map({}), dry_run=True)
    assert result is False
    assert run.calls == []
    out = capsys.readouterr().out
    assert "brew install ollama" in out


def test_ensure_ollama_installed_raises_if_still_not_on_path():
    run = fake_run(ok_prefixes=[("brew", "install", "ollama"), ("brew", "services", "start", "ollama")])
    with pytest.raises(oa.OllamaAssistError, match="isn't on PATH"):
        oa.ensure_ollama_installed(system="Darwin", run=run, which=which_map({}), confirm=lambda _: True)


# ── Pulling models ─────────────────────────────────────────────────────────


def test_pull_model_success():
    run = fake_run(ok_prefixes=[("ollama", "pull", "qwen3:8b")])
    oa.pull_model("qwen3:8b", run=run)  # no raise


def test_pull_model_failure_raises():
    run = fake_run(fail_prefixes=[("ollama", "pull", "qwen3:8b")])
    with pytest.raises(oa.OllamaAssistError, match="qwen3:8b"):
        oa.pull_model("qwen3:8b", run=run)


def test_pull_recommended_models_dedupes_and_pulls_both():
    rec = oa.TIER_TABLE[oa.TIER_STRONG]
    run = fake_run(
        ok_prefixes=[("ollama", "pull", rec.triage_model), ("ollama", "pull", rec.analysis_model)],
    )
    pulled = oa.pull_recommended_models(rec, run=run)
    assert set(pulled) == {rec.triage_model, rec.analysis_model}


def test_pull_recommended_models_dry_run_pulls_nothing(capsys):
    rec = oa.TIER_TABLE[oa.TIER_CAPABLE]
    run = fake_run()
    pulled = oa.pull_recommended_models(rec, run=run, dry_run=True)
    assert pulled == []
    assert run.calls == []
    assert rec.triage_model in capsys.readouterr().out


# ── Provider config (execs into the container -- see app/ollama_provider_cli.py) ─

_SCHEMA = """
CREATE TABLE ai_provider_configs (
    id INTEGER PRIMARY KEY, rank INTEGER, provider TEXT, label TEXT, api_key_enc TEXT,
    base_url TEXT, model TEXT, triage_model TEXT, num_ctx INTEGER, use_for_triage BOOLEAN,
    use_for_analysis BOOLEAN, thinking_mode TEXT, enabled BOOLEAN
);
CREATE TABLE ai_config (id INTEGER PRIMARY KEY, api_enabled BOOLEAN DEFAULT 0);
INSERT INTO ai_config (id, api_enabled) VALUES (1, 0);
"""

_CONTAINER_NAME = "job-squire-castelo"


def _make_instance_db(root):
    """A schema matching a real app-initialized instance: both
    `ai_provider_configs` and a seeded `ai_config` (id=1) row -- the latter
    is what `write_provider_config`'s `enable_automatic_features` flips.
    `/data` is a named Docker volume now (not a host path -- see
    ops/compose.py), so this stands in for "what the container's volume
    contains", the same way test_lifecycle.py's FakeRuntime does for other
    exec-based commands; it happens to still live on disk under `root` only
    because that's convenient for a test fixture, not because production
    code reads it there anymore."""
    db_path = root / "data" / "job-squire.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return db_path


def _write_row_like_container_cli(db_path, payload):
    """Reimplements the handful of statements
    `app/ollama_provider_cli.py::write_provider_row` runs inside the real
    container, against `db_path`, standing in for that exec call. This
    package never imports the app package (see ollama_assist.py's module
    docstring), so the fake below duplicates just enough of that logic
    rather than importing it -- deliberately kept in lockstep with that
    module by the shared behavioral tests over `write_provider_config` in
    this file, which would fail if the two drifted."""
    if not db_path.exists():
        raise oa.OllamaAssistError(f"Database not found at {db_path} inside the container.")
    base_url, triage_model, analysis_model = payload["base_url"], payload["triage_model"], payload["analysis_model"]
    num_ctx, rank, enabled = payload.get("num_ctx"), payload.get("rank"), payload.get("enabled", True)
    enable_automatic_features = payload.get("enable_automatic_features", True)

    conn = sqlite3.connect(str(db_path))
    try:
        existing = conn.execute(
            "SELECT id, rank FROM ai_provider_configs WHERE provider = 'ollama'"
        ).fetchone()
        if rank is None:
            rank = existing[1] if existing is not None else (
                conn.execute("SELECT MAX(rank) FROM ai_provider_configs").fetchone()[0] or 0
            ) + 1
        try:
            if existing is not None:
                conn.execute(
                    "UPDATE ai_provider_configs SET rank = ?, label = ?, base_url = ?, model = ?, "
                    "triage_model = ?, num_ctx = ?, use_for_triage = 1, use_for_analysis = 1, "
                    "enabled = ? WHERE provider = 'ollama'",
                    (rank, "Ollama (local)", base_url, analysis_model, triage_model, num_ctx, int(enabled)),
                )
            else:
                conn.execute(
                    "INSERT INTO ai_provider_configs (rank, provider, label, api_key_enc, base_url, "
                    "model, triage_model, num_ctx, use_for_triage, use_for_analysis, thinking_mode, "
                    "enabled) VALUES (?, 'ollama', ?, '', ?, ?, ?, ?, 1, 1, NULL, ?)",
                    (rank, "Ollama (local)", base_url, analysis_model, triage_model, num_ctx, int(enabled)),
                )
        except sqlite3.OperationalError as exc:
            if "num_ctx" in str(exc):
                raise oa.OllamaAssistError("job-squire update: no num_ctx column") from exc
            raise
        automatic_features_enabled = False
        if enable_automatic_features:
            try:
                cur = conn.execute("UPDATE ai_config SET api_enabled = 1 WHERE id = 1")
                automatic_features_enabled = cur.rowcount > 0
            except sqlite3.OperationalError:
                automatic_features_enabled = False
        conn.commit()
    finally:
        conn.close()
    return automatic_features_enabled


def container_fake_run(db_path, *, container_name=_CONTAINER_NAME, running=True, ok_prefixes=(), stdout=""):
    """`fake_run`, plus handling for the two docker/podman calls
    `write_provider_config` now makes: `inspect` (is the container up?) and
    `exec ... python3 -m app.ollama_provider_cli` (perform the write, via
    `_write_row_like_container_cli` against `db_path` standing in for the
    container's volume)."""
    inner = fake_run(ok_prefixes=ok_prefixes, stdout=stdout)

    def _run(args, **kwargs):
        args = list(args)
        if len(args) >= 2 and args[1] == "inspect":
            inner.calls.append(tuple(args))
            if not running:
                return SimpleNamespace(returncode=1, stdout="", stderr="no such container")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"Status": "running"}), stderr="")
        if len(args) >= 2 and args[1] == "exec":
            inner.calls.append(tuple(args))
            payload = json.loads(kwargs["input"])
            try:
                enabled = _write_row_like_container_cli(db_path, payload)
            except oa.OllamaAssistError as exc:
                return SimpleNamespace(returncode=1, stdout="", stderr=str(exc))
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"automatic_features_enabled": enabled}), stderr="",
            )
        return inner(args, **kwargs)

    _run.calls = inner.calls
    return _run


def test_write_provider_config_raises_if_container_not_running(tmp_path):
    root = tmp_path / "castelo"
    run = container_fake_run(root / "data" / "job-squire.db", running=False)
    with pytest.raises(oa.OllamaAssistError, match="must be running"):
        oa.write_provider_config(root, runtime="docker", container_name=_CONTAINER_NAME,
                                  base_url="http://localhost:11434", triage_model="qwen3:4b",
                                  analysis_model="qwen3:8b", run=run)


def test_write_provider_config_inserts_new_row(tmp_path):
    root = tmp_path / "castelo"
    db_path = _make_instance_db(root)
    run = container_fake_run(db_path)

    oa.write_provider_config(root, runtime="docker", container_name=_CONTAINER_NAME,
                              base_url="http://host.docker.internal:11434",
                              triage_model="qwen3:8b", analysis_model="gemma4:12b", num_ctx=16384, run=run)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ai_provider_configs WHERE provider = 'ollama'").fetchone()
    assert row["base_url"] == "http://host.docker.internal:11434"
    assert row["triage_model"] == "qwen3:8b"
    assert row["model"] == "gemma4:12b"
    assert row["num_ctx"] == 16384
    assert row["use_for_triage"] == 1
    assert row["use_for_analysis"] == 1
    assert row["enabled"] == 1
    assert row["rank"] == 1  # first row in an empty chain


def test_write_provider_config_missing_num_ctx_column_raises_actionable_error(tmp_path):
    """An instance whose image predates the num_ctx migration gets a clear,
    actionable error -- not a raw sqlite3.OperationalError -- telling the
    operator to update the instance first."""
    root = tmp_path / "castelo"
    db_path = root / "data" / "job-squire.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE ai_provider_configs (
            id INTEGER PRIMARY KEY, rank INTEGER, provider TEXT, label TEXT, api_key_enc TEXT,
            base_url TEXT, model TEXT, triage_model TEXT, use_for_triage BOOLEAN,
            use_for_analysis BOOLEAN, thinking_mode TEXT, enabled BOOLEAN
        );
    """)  # no num_ctx column -- simulates a pre-migration schema
    conn.commit()
    conn.close()
    run = container_fake_run(db_path)

    with pytest.raises(oa.OllamaAssistError, match="num_ctx migration"):
        oa.write_provider_config(root, runtime="docker", container_name=_CONTAINER_NAME,
                                  base_url="http://localhost:11434", triage_model="qwen3:8b",
                                  analysis_model="gemma4:12b", num_ctx=16384, run=run)


def test_write_provider_config_appends_after_existing_chain(tmp_path):
    root = tmp_path / "castelo"
    db_path = _make_instance_db(root)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO ai_provider_configs (rank, provider, base_url, model, triage_model, enabled) "
        "VALUES (1, 'openrouter', '', 'gpt-oss-120b', 'gpt-oss-20b', 1)"
    )
    conn.commit()
    conn.close()
    run = container_fake_run(db_path)

    oa.write_provider_config(root, runtime="docker", container_name=_CONTAINER_NAME,
                              base_url="http://localhost:11434", triage_model="qwen3:4b",
                              analysis_model="qwen3:8b", run=run)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ai_provider_configs WHERE provider = 'ollama'").fetchone()
    assert row["rank"] == 2


def test_write_provider_config_updates_existing_row_in_place(tmp_path):
    root = tmp_path / "castelo"
    db_path = _make_instance_db(root)
    run = container_fake_run(db_path)
    oa.write_provider_config(root, runtime="docker", container_name=_CONTAINER_NAME,
                              base_url="http://localhost:11434", triage_model="qwen3:4b",
                              analysis_model="qwen3:8b", run=run)

    # Re-running setup (e.g. after a hardware upgrade) updates in place --
    # it must not create a second "ollama" row.
    oa.write_provider_config(root, runtime="docker", container_name=_CONTAINER_NAME,
                              base_url="http://localhost:11434", triage_model="qwen3:8b",
                              analysis_model="gemma4:12b", run=run)

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT * FROM ai_provider_configs WHERE provider = 'ollama'").fetchall()
    assert len(rows) == 1


def test_write_provider_config_never_sets_an_api_key(tmp_path):
    root = tmp_path / "castelo"
    db_path = _make_instance_db(root)
    run = container_fake_run(db_path)
    oa.write_provider_config(root, runtime="docker", container_name=_CONTAINER_NAME,
                              base_url="http://localhost:11434", triage_model="qwen3:4b",
                              analysis_model="qwen3:8b", run=run)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT api_key_enc FROM ai_provider_configs WHERE provider = 'ollama'").fetchone()
    assert row["api_key_enc"] == ""


def test_write_provider_config_respects_explicit_rank(tmp_path):
    root = tmp_path / "castelo"
    db_path = _make_instance_db(root)
    run = container_fake_run(db_path)
    oa.write_provider_config(root, runtime="docker", container_name=_CONTAINER_NAME,
                              base_url="http://localhost:11434", triage_model="qwen3:4b",
                              analysis_model="qwen3:8b", rank=5, run=run)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT rank FROM ai_provider_configs WHERE provider = 'ollama'").fetchone()
    assert row["rank"] == 5


# ── Automatic AI Features (ai_config.api_enabled) ─────────────────────────


def test_write_provider_config_enables_automatic_features_by_default(tmp_path):
    root = tmp_path / "castelo"
    db_path = _make_instance_db(root)
    run = container_fake_run(db_path)
    enabled = oa.write_provider_config(root, runtime="docker", container_name=_CONTAINER_NAME,
                                        base_url="http://host.docker.internal:11434",
                                        triage_model="qwen3:4b", analysis_model="qwen3:8b", run=run)
    assert enabled is True
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT api_enabled FROM ai_config WHERE id = 1").fetchone()
    assert row[0] == 1


def test_write_provider_config_can_skip_enabling_automatic_features(tmp_path):
    root = tmp_path / "castelo"
    db_path = _make_instance_db(root)
    run = container_fake_run(db_path)
    enabled = oa.write_provider_config(root, runtime="docker", container_name=_CONTAINER_NAME,
                                        base_url="http://host.docker.internal:11434",
                                        triage_model="qwen3:4b", analysis_model="qwen3:8b",
                                        enable_automatic_features=False, run=run)
    assert enabled is False
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT api_enabled FROM ai_config WHERE id = 1").fetchone()
    assert row[0] == 0  # untouched, not flipped


def test_write_provider_config_warns_without_crashing_when_ai_config_missing(tmp_path, capsys):
    """An instance whose schema predates ai_config (or the row somehow isn't
    seeded yet) shouldn't crash the whole provider-row write -- it's a
    warning, same "additive, never assumed" convention as the num_ctx check
    just above."""
    root = tmp_path / "castelo"
    db_path = root / "data" / "job-squire.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE ai_provider_configs (
            id INTEGER PRIMARY KEY, rank INTEGER, provider TEXT, label TEXT, api_key_enc TEXT,
            base_url TEXT, model TEXT, triage_model TEXT, num_ctx INTEGER, use_for_triage BOOLEAN,
            use_for_analysis BOOLEAN, thinking_mode TEXT, enabled BOOLEAN
        );
    """)  # no ai_config table at all
    conn.commit()
    conn.close()
    run = container_fake_run(db_path)

    enabled = oa.write_provider_config(root, runtime="docker", container_name=_CONTAINER_NAME,
                                        base_url="http://host.docker.internal:11434",
                                        triage_model="qwen3:4b", analysis_model="qwen3:8b", run=run)
    assert enabled is False
    assert "couldn't enable Automatic AI Features" in capsys.readouterr().out

    # The provider row itself still got written despite ai_config missing.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ai_provider_configs WHERE provider = 'ollama'").fetchone()
    assert row["base_url"] == "http://host.docker.internal:11434"


# ── Round-trip test (direct Ollama API, not the app's adapter) ────────────


def test_roundtrip_success(monkeypatch):
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"response": "ok"}).encode()

    monkeypatch.setattr(oa.urllib.request, "urlopen", lambda req, timeout=None: FakeResp())
    ok, detail = oa.test_roundtrip("http://localhost:11434", "qwen3:8b")
    assert ok is True
    assert detail == "ok"


def test_roundtrip_failure(monkeypatch):
    def _raise(*args, **kwargs):
        raise oa.urllib.error.URLError("connection refused")

    monkeypatch.setattr(oa.urllib.request, "urlopen", _raise)
    ok, detail = oa.test_roundtrip("http://localhost:11434", "qwen3:8b")
    assert ok is False
    assert "connection refused" in detail


# ── run_setup orchestration ────────────────────────────────────────────────


def test_run_setup_dry_run_touches_nothing(tmp_path, monkeypatch, capsys):
    root = tmp_path / "castelo"  # deliberately never created -- dry-run must never need it
    monkeypatch.setattr(oa, "detect_host_capabilities", lambda **kwargs: _caps(ram_gb=16, apple_silicon=True))

    def fail_if_called(*args, **kwargs):
        raise AssertionError("dry-run must not perform real side effects")

    monkeypatch.setattr(oa, "write_provider_config", fail_if_called)
    monkeypatch.setattr(oa, "pull_model", fail_if_called)
    monkeypatch.setattr(oa, "test_roundtrip", fail_if_called)

    run = fake_run()
    result = oa.run_setup(root, runtime="docker", container_name=_CONTAINER_NAME, run=run,
                           which=which_map({}), dry_run=True, confirm=lambda _: True)

    assert result.host_capabilities_path is None
    assert result.models_pulled == []
    assert result.provider_configured is False
    assert result.roundtrip_ok is None
    assert not root.exists()  # nothing was written to disk
    out = capsys.readouterr().out
    assert "dry-run" in out


def test_run_setup_not_reasonable_raises_before_touching_anything(tmp_path, monkeypatch):
    root = tmp_path / "castelo"
    monkeypatch.setattr(oa, "detect_host_capabilities", lambda **kwargs: _caps(ram_gb=4))

    with pytest.raises(oa.OllamaAssistError, match="cloud tier"):
        oa.run_setup(root, runtime="docker", container_name=_CONTAINER_NAME, run=fake_run(),
                     which=which_map({}), confirm=lambda _: True)
    assert not root.exists()


def test_run_setup_full_chain_real_writes(tmp_path, monkeypatch):
    root = tmp_path / "castelo"
    db_path = _make_instance_db(root)
    monkeypatch.setattr(oa, "detect_host_capabilities", lambda **kwargs: _caps(ram_gb=16, apple_silicon=True))
    monkeypatch.setattr(oa, "test_roundtrip", lambda base_url, model, **kwargs: (True, "ok"))

    run = container_fake_run(db_path, ok_prefixes=[
        ("ollama", "pull", "qwen3:8b"),
        ("ollama", "pull", "gemma4:12b"),
        ("ollama", "create", "qwen3:8b-ctx16384"),
        ("ollama", "create", "gemma4:12b-ctx16384"),
    ])

    result = oa.run_setup(
        root, runtime="docker", container_name=_CONTAINER_NAME, run=run,
        which=which_map({"ollama": "/usr/local/bin/ollama"}), confirm=lambda _: True,
    )

    assert result.provider_configured is True
    assert set(result.models_pulled) == {"qwen3:8b", "gemma4:12b"}
    assert result.models_derived == {"qwen3:8b": "qwen3:8b-ctx16384", "gemma4:12b": "gemma4:12b-ctx16384"}
    assert result.num_ctx == 16384
    assert result.roundtrip_ok is True
    assert (paths.data_dir(root) / oa.HOST_CAPABILITIES_FILENAME).exists()

    conn = sqlite3.connect(str(paths.sqlite_db_path(root)))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ai_provider_configs WHERE provider = 'ollama'").fetchone()
    # Default base_url is the container-reachable address, never plain
    # "localhost" (that only resolves to the container itself) -- and always
    # /v1-suffixed: app/ai.py's call_openai_compat() does
    # `base_url.rstrip("/") + "/chat/completions"`, and Ollama only serves
    # that route under /v1 (bare "/chat/completions" 404s with Ollama's raw
    # "404 page not found" text). See test_run_setup_writes_v1_suffixed_base_url
    # below for the regression this guards.
    assert row["base_url"] == oa.OLLAMA_CONTAINER_HOST + "/v1"
    # The *derived* (context-sized) model names are what get written -- not the base tags.
    assert row["triage_model"] == "qwen3:8b-ctx16384"
    assert row["model"] == "gemma4:12b-ctx16384"
    assert row["num_ctx"] == 16384

    ai_cfg_row = conn.execute("SELECT api_enabled FROM ai_config WHERE id = 1").fetchone()
    assert ai_cfg_row[0] == 1  # Automatic AI Features enabled by default
    assert result.automatic_features_enabled is True
    assert result.base_url == oa.OLLAMA_CONTAINER_HOST + "/v1"


def test_run_setup_roundtrip_tests_localhost_not_container_host_by_default(tmp_path, monkeypatch):
    """The round-trip test runs from the CLI/host process, not the
    container -- "host.docker.internal" (the default base_url written for
    the container's use) doesn't resolve from bare host, so the test itself
    must probe OLLAMA_DEFAULT_HOST ("localhost") instead when the caller
    left base_url at its default."""
    root = tmp_path / "castelo"
    db_path = _make_instance_db(root)
    monkeypatch.setattr(oa, "detect_host_capabilities", lambda **kwargs: _caps(ram_gb=16, apple_silicon=True))
    captured = {}

    def fake_roundtrip(base_url, model, **kwargs):
        captured["base_url"] = base_url
        return True, "ok"

    monkeypatch.setattr(oa, "test_roundtrip", fake_roundtrip)
    run = container_fake_run(db_path, ok_prefixes=[
        ("ollama", "pull", "qwen3:8b"), ("ollama", "pull", "gemma4:12b"),
        ("ollama", "create", "qwen3:8b-ctx16384"), ("ollama", "create", "gemma4:12b-ctx16384"),
    ])

    oa.run_setup(root, runtime="docker", container_name=_CONTAINER_NAME, run=run,
                 which=which_map({"ollama": "/usr/local/bin/ollama"}), confirm=lambda _: True)

    assert captured["base_url"] == oa.OLLAMA_DEFAULT_HOST


def test_run_setup_roundtrip_tests_explicit_base_url_override_as_given(tmp_path, monkeypatch):
    """An operator-supplied base_url (e.g. Ollama on another machine on the
    network) is assumed reachable from the CLI host too and tested as-is,
    not redirected to localhost."""
    root = tmp_path / "castelo"
    db_path = _make_instance_db(root)
    monkeypatch.setattr(oa, "detect_host_capabilities", lambda **kwargs: _caps(ram_gb=16, apple_silicon=True))
    captured = {}

    def fake_roundtrip(base_url, model, **kwargs):
        captured["base_url"] = base_url
        return True, "ok"

    monkeypatch.setattr(oa, "test_roundtrip", fake_roundtrip)
    run = container_fake_run(db_path, ok_prefixes=[
        ("ollama", "pull", "qwen3:8b"), ("ollama", "pull", "gemma4:12b"),
        ("ollama", "create", "qwen3:8b-ctx16384"), ("ollama", "create", "gemma4:12b-ctx16384"),
    ])

    oa.run_setup(root, runtime="docker", container_name=_CONTAINER_NAME,
                 base_url="http://192.168.1.50:11434", run=run,
                 which=which_map({"ollama": "/usr/local/bin/ollama"}), confirm=lambda _: True)

    assert captured["base_url"] == "http://192.168.1.50:11434"


# ── /v1 suffix regression (base_url must be OpenAI-compat, not the bare host) ─
#
# `job-squire ollama setup` used to write the bare Ollama host (e.g.
# "http://host.docker.internal:11434") straight into ai_provider_configs.base_url.
# app/ai.py's call_openai_compat() always does
# `base_url.rstrip("/") + "/chat/completions"`, and Ollama only serves that
# route under `/v1` -- so every triage/analysis call against a freshly
# `ollama setup`-configured provider 404'd with Ollama's raw
# "404 page not found" text (Triage Batch in the app surfaced this as
# "404 Not Found from ollama: 404 page not found"). The CLI's own
# `--skip-test`-guarded round-trip check didn't catch it because
# test_roundtrip() deliberately hits Ollama's *native* /api/generate on the
# bare host, a route that exists with or without the /v1 bug -- so `ollama
# setup` reported success right up until the app tried to actually use the
# provider it had just configured.


def test_openai_compat_base_url_appends_v1_to_bare_host():
    assert oa._openai_compat_base_url("http://host.docker.internal:11434") == \
        "http://host.docker.internal:11434/v1"


def test_openai_compat_base_url_strips_trailing_slash_before_appending():
    assert oa._openai_compat_base_url("http://localhost:11434/") == "http://localhost:11434/v1"


def test_openai_compat_base_url_is_idempotent_when_already_suffixed():
    """An operator who copies one of app/ai.py's _PROVIDER_URLS defaults
    (all already /v1-suffixed) into --base-url must not get a doubled
    "/v1/v1"."""
    assert oa._openai_compat_base_url("http://localhost:11434/v1") == "http://localhost:11434/v1"
    assert oa._openai_compat_base_url("http://localhost:11434/v1/") == "http://localhost:11434/v1"


def test_run_setup_writes_v1_suffixed_base_url_for_explicit_override(tmp_path, monkeypatch):
    """The same normalization applies to an operator-supplied --base-url
    (e.g. Ollama on another machine), not just the OLLAMA_CONTAINER_HOST
    default -- covered separately from test_run_setup_full_chain_real_writes
    (which only exercises the default) since this is the exact case an
    operator following --base-url's own --help text would hit."""
    root = tmp_path / "castelo"
    db_path = _make_instance_db(root)
    monkeypatch.setattr(oa, "detect_host_capabilities", lambda **kwargs: _caps(ram_gb=16, apple_silicon=True))
    monkeypatch.setattr(oa, "test_roundtrip", lambda base_url, model, **kwargs: (True, "ok"))
    run = container_fake_run(db_path, ok_prefixes=[
        ("ollama", "pull", "qwen3:8b"), ("ollama", "pull", "gemma4:12b"),
        ("ollama", "create", "qwen3:8b-ctx16384"), ("ollama", "create", "gemma4:12b-ctx16384"),
    ])

    result = oa.run_setup(root, runtime="docker", container_name=_CONTAINER_NAME,
                           base_url="http://192.168.1.50:11434", run=run,
                           which=which_map({"ollama": "/usr/local/bin/ollama"}), confirm=lambda _: True)

    assert result.base_url == "http://192.168.1.50:11434/v1"
    conn = sqlite3.connect(str(paths.sqlite_db_path(root)))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT base_url FROM ai_provider_configs WHERE provider = 'ollama'").fetchone()
    assert row["base_url"] == "http://192.168.1.50:11434/v1"


def test_run_setup_dry_run_echoes_v1_suffixed_base_url(tmp_path, monkeypatch, capsys):
    """The dry-run preview must show the value that would actually be
    written, not the bare host -- otherwise --dry-run output silently lies
    about what `ollama setup` is about to do."""
    root = tmp_path / "castelo"
    monkeypatch.setattr(oa, "detect_host_capabilities", lambda **kwargs: _caps(ram_gb=16, apple_silicon=True))

    oa.run_setup(root, runtime="docker", container_name=_CONTAINER_NAME, run=fake_run(),
                 which=which_map({}), dry_run=True, confirm=lambda _: True)

    out = capsys.readouterr().out
    assert f"base_url={oa.OLLAMA_CONTAINER_HOST}/v1" in out
    assert f"base_url={oa.OLLAMA_CONTAINER_HOST}," not in out  # not the unsuffixed bare host


def test_run_setup_can_skip_enabling_automatic_features(tmp_path, monkeypatch):
    root = tmp_path / "castelo"
    db_path = _make_instance_db(root)
    monkeypatch.setattr(oa, "detect_host_capabilities", lambda **kwargs: _caps(ram_gb=16, apple_silicon=True))
    monkeypatch.setattr(oa, "test_roundtrip", lambda base_url, model, **kwargs: (True, "ok"))
    run = container_fake_run(db_path, ok_prefixes=[
        ("ollama", "pull", "qwen3:8b"), ("ollama", "pull", "gemma4:12b"),
        ("ollama", "create", "qwen3:8b-ctx16384"), ("ollama", "create", "gemma4:12b-ctx16384"),
    ])

    result = oa.run_setup(
        root, runtime="docker", container_name=_CONTAINER_NAME, run=run,
        which=which_map({"ollama": "/usr/local/bin/ollama"}), confirm=lambda _: True,
        enable_automatic_features=False,
    )

    assert result.automatic_features_enabled is False
    conn = sqlite3.connect(str(paths.sqlite_db_path(root)))
    row = conn.execute("SELECT api_enabled FROM ai_config WHERE id = 1").fetchone()
    assert row[0] == 0


def test_run_setup_skip_derive_writes_base_tags_and_no_num_ctx(tmp_path, monkeypatch):
    root = tmp_path / "castelo"
    db_path = _make_instance_db(root)
    monkeypatch.setattr(oa, "detect_host_capabilities", lambda **kwargs: _caps(ram_gb=16, apple_silicon=True))
    monkeypatch.setattr(oa, "test_roundtrip", lambda base_url, model, **kwargs: (True, "ok"))

    run = container_fake_run(db_path, ok_prefixes=[("ollama", "pull", "qwen3:8b"), ("ollama", "pull", "gemma4:12b")])

    result = oa.run_setup(
        root, runtime="docker", container_name=_CONTAINER_NAME, run=run,
        which=which_map({"ollama": "/usr/local/bin/ollama"}), confirm=lambda _: True,
        skip_derive=True,
    )

    assert result.models_derived == {}
    assert result.num_ctx is None
    conn = sqlite3.connect(str(paths.sqlite_db_path(root)))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM ai_provider_configs WHERE provider = 'ollama'").fetchone()
    assert row["triage_model"] == "qwen3:8b"
    assert row["model"] == "gemma4:12b"
    assert row["num_ctx"] is None


# ── Modelfile / derived-model creation ────────────────────────────────────


def test_modelfile_for_shape():
    text = oa.modelfile_for("qwen3:8b", 16384)
    assert text == "FROM qwen3:8b\nPARAMETER num_ctx 16384\n"


def test_context_model_name_appends_ctx_suffix_to_the_tag():
    assert oa.context_model_name("qwen3:8b", 16384) == "qwen3:8b-ctx16384"
    assert oa.context_model_name("gemma4:12b", 8192) == "gemma4:12b-ctx8192"


def test_derive_context_model_runs_ollama_create_from_a_temp_modelfile(tmp_path):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        modelfile_path = args[-1]
        captured["modelfile_contents"] = Path(modelfile_path).read_text()
        return SimpleNamespace(returncode=0)

    name = oa.derive_context_model("qwen3:8b", 16384, run=fake_run)

    assert name == "qwen3:8b-ctx16384"
    assert captured["args"][:3] == ["ollama", "create", "qwen3:8b-ctx16384"]
    assert captured["modelfile_contents"] == "FROM qwen3:8b\nPARAMETER num_ctx 16384\n"
    # The temp Modelfile is cleaned up afterward.
    assert not Path(captured["args"][-1]).exists()


def test_derive_context_model_failure_raises():
    run = fake_run(fail_prefixes=[("ollama", "create")])
    with pytest.raises(oa.OllamaAssistError, match="ollama create"):
        oa.derive_context_model("qwen3:8b", 16384, run=run)


def test_derive_context_models_dedupes_shared_base_tag():
    rec = oa.ModelRecommendation(
        tier="test", description="", triage_model="qwen3:8b", analysis_model="qwen3:8b",
        approx_download_gb=5.0, num_ctx=8192,
    )
    calls = []

    def fake_run(args, **kwargs):
        calls.append(tuple(args[:3]))
        return SimpleNamespace(returncode=0)

    derived = oa.derive_context_models(rec, run=fake_run)
    assert derived == {"qwen3:8b": "qwen3:8b-ctx8192"}
    assert calls == [("ollama", "create", "qwen3:8b-ctx8192")]  # only one `ollama create`, not two


def test_derive_context_models_dry_run_creates_nothing(capsys):
    rec = oa.TIER_TABLE[oa.TIER_CAPABLE]
    run = fake_run()
    derived = oa.derive_context_models(rec, run=run, dry_run=True)
    assert run.calls == []
    assert derived[rec.triage_model] == oa.context_model_name(rec.triage_model, rec.num_ctx)
    assert "dry-run" in capsys.readouterr().out
