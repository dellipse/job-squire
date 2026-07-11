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
"""job_squire_cli.query.mcp_client -- result decoding and end-to-end calls.

_extract_result's cases are pinned directly against real mcp.types objects
built the way FastMCP actually builds them (verified empirically against
mcp==1.28.1 with an in-memory server -- see the module docstring), rather
than against hand-rolled fakes, so a future `mcp` upgrade that changes the
wrapping is caught here instead of only in production.

The end-to-end tests spin up a real FastMCP app (Streamable HTTP) in a
background thread on a loopback port and drive it through call_tool() and
check_health() exactly as the query commands do -- no Hermes, no
~/.hermes/, nothing but this module and a live instance.
"""
import threading
import time

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from job_squire_cli.query import mcp_client
from job_squire_cli.query.mcp_client import MCPError, _extract_result


# ---------------------------------------------------------------------------
# _extract_result: pinned against real FastMCP wrapping behavior
# ---------------------------------------------------------------------------

def test_dict_return_type_single_text_block():
    result = CallToolResult(
        content=[TextContent(type="text", text='{"jobs": [], "count": 0}')],
        structuredContent=None,
        isError=False,
    )
    assert _extract_result(result, "get_pipeline") == {"jobs": [], "count": 0}


def test_list_return_type_one_block_per_item():
    result = CallToolResult(
        content=[
            TextContent(type="text", text='{"id": 1}'),
            TextContent(type="text", text='{"id": 2}'),
        ],
        structuredContent=None,
        isError=False,
    )
    assert _extract_result(result, "list_jobs") == [{"id": 1}, {"id": 2}]


def test_list_return_type_single_item_stays_a_list():
    # Regression: a one-item list produces exactly one content block, the
    # same wire shape as a dict return -- only the LIST_RETURNING_TOOLS
    # hint (keyed off the tool name) disambiguates it. Caught by the
    # end-to-end test_call_tool_round_trip fixture below, which returns
    # exactly one job.
    result = CallToolResult(
        content=[TextContent(type="text", text='{"id": 1}')],
        structuredContent=None,
        isError=False,
    )
    assert _extract_result(result, "list_jobs") == [{"id": 1}]
    # The same single-block shape from a *dict*-returning tool must stay a
    # bare dict -- the hint is keyed by tool name, not block count.
    assert _extract_result(result, "get_job") == {"id": 1}


def test_empty_list_return_type_zero_blocks():
    result = CallToolResult(content=[], structuredContent=None, isError=False)
    assert _extract_result(result, "list_jobs") == []


def test_str_return_type_wrapped_in_structured_content():
    result = CallToolResult(
        content=[TextContent(type="text", text="# Profile\nmarkdown")],
        structuredContent={"result": "# Profile\nmarkdown"},
        isError=False,
    )
    assert _extract_result(result, "get_candidate_profile") == "# Profile\nmarkdown"


def test_error_result_raises_mcp_error_with_message():
    result = CallToolResult(
        content=[TextContent(type="text", text="Error executing tool boom: job not found")],
        structuredContent=None,
        isError=True,
    )
    with pytest.raises(MCPError, match="job not found"):
        _extract_result(result, "boom")


# ---------------------------------------------------------------------------
# End-to-end over real Streamable HTTP, no Hermes anywhere
# ---------------------------------------------------------------------------

@pytest.fixture
def running_server():
    mcp = FastMCP("test-job-squire")

    @mcp.tool()
    def list_jobs(status: str = "") -> list:
        jobs = [{"id": 1, "title": "Engineer", "status": "Saved"}]
        return [j for j in jobs if not status or j["status"] == status]

    @mcp.tool()
    def boom() -> dict:
        raise ValueError("nope")

    app = mcp.streamable_http_app()

    async def asgi(scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/health":
            body = b'{"ok": true}'
            await send({
                "type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})
            return
        scope = {**scope, "path": "/mcp", "raw_path": b"/mcp"}
        await app(scope, receive, send)

    config = uvicorn.Config(asgi, host="127.0.0.1", port=18321, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)

    yield "http://127.0.0.1:18321"

    server.should_exit = True
    thread.join(timeout=5)


def test_check_health_true_when_server_up(running_server):
    assert mcp_client.check_health(running_server) is True


def test_check_health_false_when_nothing_listening():
    assert mcp_client.check_health("http://127.0.0.1:1") is False


def test_call_tool_round_trip(running_server):
    result = mcp_client.call_tool(running_server, None, "list_jobs", {"status": "Saved"})
    assert result == [{"id": 1, "title": "Engineer", "status": "Saved"}]


def test_call_tool_error_surfaces_as_mcp_error(running_server):
    with pytest.raises(MCPError, match="nope"):
        mcp_client.call_tool(running_server, None, "boom", {})


def test_call_tool_connection_refused_raises_mcp_error():
    with pytest.raises(MCPError):
        mcp_client.call_tool("http://127.0.0.1:1", None, "list_jobs", {}, timeout=2.0)
