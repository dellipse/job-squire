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
"""Query command output, with mcp_client.call_tool faked out.

These exercise the settled command set end to end through click, the same
way a user invokes `job-squire query ...`, without needing a live server --
mcp_client.call_tool is monkeypatched per test to return exactly what a
real instance's tool would.
"""
import json

import click.testing
import pytest

from job_squire_cli.query import commands, mcp_client
from job_squire_cli.query.config import QueryConfig


@pytest.fixture(autouse=True)
def fake_config(monkeypatch):
    monkeypatch.setattr(
        commands, "load_query_config",
        lambda instance=None: QueryConfig(endpoint="http://localhost:9000", token="jsq_mcp_test"),
    )


def _fake_call_tool(responses):
    def _call(endpoint, token, name, arguments=None, timeout=30.0):
        if name not in responses:
            raise AssertionError(f"unexpected tool call: {name}")
        return responses[name]
    return _call


def test_pipeline_json(monkeypatch):
    monkeypatch.setattr(mcp_client, "call_tool", _fake_call_tool({
        "get_pipeline": {"jobs": [{"status": "Saved"}, {"status": "Saved"}, {"status": "Applied"}]},
    }))
    runner = click.testing.CliRunner()
    result = runner.invoke(commands.query, ["--json", "pipeline"])
    assert result.exit_code == 0
    assert json.loads(result.output) == {"Applied": 1, "Saved": 2}


def test_list_table_output(monkeypatch):
    monkeypatch.setattr(mcp_client, "call_tool", _fake_call_tool({
        "list_jobs": [{"id": 1, "title": "Engineer", "company": "Acme", "ai_fit_score": 9}],
    }))
    runner = click.testing.CliRunner()
    result = runner.invoke(commands.query, ["list", "Saved"])
    assert result.exit_code == 0
    assert "Engineer" in result.output
    assert "Acme" in result.output


def test_list_active_pseudo_stage_filters_client_side(monkeypatch):
    monkeypatch.setattr(mcp_client, "call_tool", _fake_call_tool({
        "list_jobs": [
            {"id": 1, "title": "Active One", "status": "Saved"},
            {"id": 2, "title": "Dead One", "status": "Rejected"},
        ],
    }))
    runner = click.testing.CliRunner()
    result = runner.invoke(commands.query, ["--json", "list", "active"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert [j["id"] for j in data] == [1]


def test_followups_reports_none_cleanly(monkeypatch):
    monkeypatch.setattr(mcp_client, "call_tool", _fake_call_tool({
        "list_overdue_followups": {"jobs": [], "submissions": []},
    }))
    runner = click.testing.CliRunner()
    result = runner.invoke(commands.query, ["followups"])
    assert result.exit_code == 0
    assert "No overdue follow-ups" in result.output


def test_job_detail_not_found(monkeypatch):
    monkeypatch.setattr(mcp_client, "call_tool", _fake_call_tool({"get_job": {}}))
    runner = click.testing.CliRunner()
    result = runner.invoke(commands.query, ["job", "42"])
    assert result.exit_code == 0
    assert "not found" in result.output


def test_contact_detail(monkeypatch):
    monkeypatch.setattr(mcp_client, "call_tool", _fake_call_tool({
        "get_contact": {"id": 7, "name": "Jane Recruiter", "submissions": []},
    }))
    runner = click.testing.CliRunner()
    result = runner.invoke(commands.query, ["contact", "7"])
    assert result.exit_code == 0
    assert "Jane Recruiter" in result.output


def test_error_from_mcp_client_prints_clean_message_not_traceback(monkeypatch):
    def _raise(*a, **k):
        raise mcp_client.MCPError("server unreachable")
    monkeypatch.setattr(mcp_client, "call_tool", _raise)
    runner = click.testing.CliRunner()
    result = runner.invoke(commands.query, ["pipeline"])
    assert result.exit_code == 1
    assert "server unreachable" in result.output
    assert "Traceback" not in result.output


def test_health_reports_ok(monkeypatch):
    monkeypatch.setattr(mcp_client, "check_health", lambda endpoint, timeout=10.0: True)
    monkeypatch.setattr(mcp_client, "call_tool", _fake_call_tool({
        "list_jobs": [{"id": 1}],
    }))
    runner = click.testing.CliRunner()
    result = runner.invoke(commands.query, ["health"])
    assert result.exit_code == 0
    assert "Server OK" in result.output
    assert "MCP OK" in result.output


def test_health_fails_cleanly_when_server_down(monkeypatch):
    monkeypatch.setattr(mcp_client, "check_health", lambda endpoint, timeout=10.0: False)
    runner = click.testing.CliRunner()
    result = runner.invoke(commands.query, ["health"])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_no_config_gives_actionable_error_not_traceback(monkeypatch):
    from job_squire_cli.query.config import QueryConfigError

    def _raise(instance=None):
        raise QueryConfigError("No MCP endpoint configured. Set JOB_SQUIRE_MCP_URL ...")
    monkeypatch.setattr(commands, "load_query_config", _raise)
    runner = click.testing.CliRunner()
    result = runner.invoke(commands.query, ["pipeline"])
    assert result.exit_code == 1
    assert "JOB_SQUIRE_MCP_URL" in result.output
    assert "Traceback" not in result.output
