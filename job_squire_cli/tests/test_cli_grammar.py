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
"""The settled command grammar: deployment verbs flat, query namespaced.

These tests don't need the [query] extra installed and must keep passing
without it -- if they ever import job_squire_cli.query.commands eagerly,
the whole point of the lazy group is defeated.

Prompt C5 made create/start/stop/restart/status/list/remove real (see
ops/lifecycle.py and tests/test_lifecycle.py, tests/test_ops_commands.py
for their behavior). Prompt C6 made `configure` real too (see
tests/test_configure.py). Prompt C7 made `update` and `adopt` real (see
tests/test_lifecycle.py and tests/test_ops_commands.py). Prompt C8 made
`backup`/`restore` real too (see tests/test_backup.py) -- every deployment
verb is real as of this prompt, so there is no longer a stub set here.
"""
import subprocess
import sys

import click.testing

from job_squire_cli.cli import main

DEPLOYMENT_COMMANDS = [
    "create", "start", "stop", "restart", "status", "list",
    "update", "remove", "uninstall", "adopt", "configure", "backup", "restore", "proxy",
]


def test_top_level_lists_deployment_commands_and_query_group():
    runner = click.testing.CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for name in DEPLOYMENT_COMMANDS:
        assert name in result.output
    assert "query" in result.output


def test_lazy_import_in_subprocess_never_touches_query_stack():
    """The real proof that the query group is lazy: a fresh interpreter
    (so no other test's imports can contaminate sys.modules), a deployment
    command, and an assertion that neither `rich` nor `mcp` ever loaded.

    A same-process sys.modules check would be meaningless here: pytest
    collects every test *module* up front, and this suite's own
    test_mcp_client.py and test_query_commands.py legitimately import
    job_squire_cli.query.commands/mcp_client (and mcp itself pulls in rich
    for its logging), so by the time any test function runs, those are
    already cached regardless of whether lazy-loading works. Only a
    separate process proves the claim.
    """
    # 'status' with no NAME lists registered instances and needs no
    # instance to exist (an empty registry just prints "No instances
    # registered."), so it's a real deployment command that never touches
    # the query stack and never prompts -- exactly what this test needs.
    script = (
        "import sys, tempfile, os\n"
        "os.environ['XDG_CONFIG_HOME'] = tempfile.mkdtemp()\n"
        "from click.testing import CliRunner\n"
        "from job_squire_cli.cli import main\n"
        "result = CliRunner().invoke(main, ['status'])\n"
        "assert result.exit_code == 0, result.output\n"
        "assert 'rich' not in sys.modules, 'rich should not be imported for a deployment command'\n"
        "assert 'mcp' not in sys.modules, 'mcp should not be imported for a deployment command'\n"
        "assert 'job_squire_cli.query.commands' not in sys.modules\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout


def test_query_group_help_lists_its_own_options_through_the_lazy_wrapper():
    """Regression test: `_LazyGroup` only overrode list_commands/get_command,
    so `self.params` was always the empty list the wrapper was constructed
    with -- `query --help` silently omitted `--json`/`--instance` and
    `query --instance NAME health` failed with "No such option
    '--instance'". Must go through `main` (the real top-level lazy-wrapped
    group), not `job_squire_cli.query.commands.query` directly -- the
    other query tests invoke that real group and would never have caught
    this."""
    runner = click.testing.CliRunner()
    result = runner.invoke(main, ["query", "--help"])
    assert result.exit_code == 0
    assert "--instance" in result.output
    assert "--json" in result.output


def test_query_instance_option_is_parsed_by_the_lazy_wrapper(monkeypatch, tmp_path):
    # Isolate from this machine's real ~/.config or ~/Library config file --
    # "does-not-exist" would never coincidentally be configured there, but
    # pin it anyway rather than depend on ambient state.
    from job_squire_cli.query import config as config_module

    monkeypatch.setattr(config_module.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("JOB_SQUIRE_MCP_URL", raising=False)
    monkeypatch.delenv("JOB_SQUIRE_MCP_TOKEN", raising=False)

    runner = click.testing.CliRunner()
    result = runner.invoke(main, ["query", "--instance", "does-not-exist", "health"])
    # The option itself must parse -- reaching an MCP-config resolution
    # error (not "No such option '--instance'") is the proof this test is
    # after; the exact wording depends on whether anything is configured.
    assert "No such option" not in result.output
    assert result.exit_code == 1
    assert "No MCP endpoint configured" in result.output


def test_version_flag_reports_unified_scheme():
    import re

    runner = click.testing.CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "job-squire, version" in result.output
    # PEP 440 local-version scheme: <base>+<local-label>, e.g. 0.5.0+abc1234
    # or the 0.0.0+unknown fallback -- never the OCI-tag hyphen form.
    match = re.search(r"version (\S+)", result.output)
    assert match, result.output
    assert re.fullmatch(r"\d+\.\d+\.\d+\+[0-9a-zA-Z]+", match.group(1)), match.group(1)


def test_console_scripts_wire_to_the_same_entry_point():
    import importlib.metadata

    eps = importlib.metadata.entry_points(group="console_scripts")
    targets = {ep.name: ep.value for ep in eps if ep.name in ("job-squire", "jobsquire")}
    assert targets.get("job-squire") == "job_squire_cli.cli:main"
    assert targets.get("jobsquire") == "job_squire_cli.cli:main"
