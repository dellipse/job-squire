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
"""A self-contained MCP client -- no Hermes, no ~/.hermes/ sidecar.

Talks Streamable HTTP directly to app/mcp_server.py's `/mcp` endpoint using
the same `mcp` library the server depends on (see requirements.txt), with
the bearer token in the Authorization header exactly as the server expects
(app/mcp_server.py's `_extract_bearer` / the static jsq_mcp_ token, or an
OAuth access token -- either way it's just a bearer string to this client).

Result decoding is deliberately defensive rather than assuming one shape,
because FastMCP's actual wrapping of a tool's return value into
CallToolResult.content / .structuredContent differs by return-type
annotation (verified empirically against mcp==1.28.1 with an in-memory
FastMCP server -- see tests/test_mcp_client.py for the fixtures that pin
this down):

  - `-> dict`  tools (get_pipeline, get_job, ...): structuredContent is
    None; content is exactly one TextContent block holding the JSON-encoded
    dict.
  - `-> list`  tools (list_jobs, list_contacts, ...): structuredContent is
    None; content is *one TextContent block per list item* (FastMCP's
    backwards-compatible ad hoc conversion recurses into the list), so the
    list has to be reassembled from all of them.
  - `-> str`   tools (get_candidate_profile, get_kit_instructions):
    structuredContent is `{"result": "<the string>"}`; content is a single
    TextContent holding the raw (unencoded) string.
  - An empty list return produces zero content blocks.
  - A tool that raises produces `isError=True` with the error message as
    the single content block's text.

One shape is genuinely ambiguous from content alone: a `-> list` tool
returning exactly one item produces the same single-TextContent-block
shape as a `-> dict` tool, since FastMCP serializes each list item as its
own block with no wrapping array marker. There's no way to tell those
apart from the wire format, so LIST_RETURNING_TOOLS below names the tools
we know return a list (from their app/mcp_server.py annotations) and
_decode_content uses that as the tie-breaker instead of guessing.

_extract_result() below handles all five without guessing which annotation
a given tool used.
"""
import json
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.types import CallToolResult, TextContent

MCP_PATH = "/mcp"


class MCPError(RuntimeError):
    """Raised for a tool-level error or a transport failure."""


def check_health(endpoint: str, timeout: float = 10.0) -> bool:
    """Hit the server's plain, unauthenticated /health endpoint.

    This is a distinct, non-MCP check (app/mcp_server.py answers it before
    any auth is evaluated), so it tells you the server process is up even
    before you know whether your token is valid.
    """
    url = endpoint.rstrip("/") + "/health"
    try:
        with urlopen(Request(url), timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (URLError, TimeoutError, OSError, ValueError):
        return False
    return bool(data.get("ok"))


def call_tool(
    endpoint: str,
    token: str | None,
    name: str,
    arguments: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> Any:
    """Synchronously invoke one MCP tool. Raises MCPError on any failure."""
    import anyio

    try:
        return anyio.run(_call_tool_async, endpoint, token, name, arguments or {}, timeout)
    except* MCPError as eg:
        # Raised deliberately by _extract_result, outside any task group, but
        # except* auto-wraps even a plain exception for matching purposes
        # (see PEP 654) -- eg.exceptions[0] unwraps it back to the original.
        raise eg.exceptions[0] from None
    except* Exception as eg:
        # Transport/protocol failures (connection refused, TLS error, bad
        # handshake, ...) surface through anyio's TaskGroup as an
        # ExceptionGroup even for a single failure; report the first cause
        # in one clean, uniform error.
        raise MCPError(f"MCP call '{name}' failed: {eg.exceptions[0]}") from eg.exceptions[0]


async def _call_tool_async(
    endpoint: str, token: str | None, name: str, arguments: dict[str, Any], timeout: float
) -> Any:
    url = endpoint.rstrip("/") + MCP_PATH
    headers = {"Authorization": f"Bearer {token}"} if token else None
    http_client = create_mcp_http_client(headers=headers, timeout=httpx.Timeout(timeout))

    async with http_client:
        async with streamable_http_client(url, http_client=http_client) as (
            read,
            write,
            _get_session_id,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)

    return _extract_result(result, name)


# Tool names known to be declared `-> list` on the server (app/mcp_server.py).
# This is what breaks the single-item-list-vs-dict ambiguity below: with
# exactly one list item, FastMCP's ad hoc content conversion produces the
# same single-TextContent-block shape as a dict return, so the tool name is
# the only reliable signal that content should stay a list of one instead
# of collapsing to a bare dict. Keep this in sync with app/mcp_server.py's
# tool return annotations if new list-returning tools are added there.
LIST_RETURNING_TOOLS = frozenset({"list_jobs", "list_contacts", "list_unanalyzed_jobs"})


def _extract_result(result: CallToolResult, tool_name: str) -> Any:
    if result.isError:
        message = result.content[0].text if result.content else "unknown error"
        raise MCPError(f"MCP error [{tool_name}]: {message}")

    if result.structuredContent is not None:
        sc = result.structuredContent
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc

    return _decode_content(result.content, is_list=tool_name in LIST_RETURNING_TOOLS)


def _decode_content(blocks: list, is_list: bool = False) -> Any:
    if not blocks:
        return [] if is_list else None
    texts = [b.text for b in blocks if isinstance(b, TextContent)]
    if len(texts) == 1 and not is_list:
        try:
            return json.loads(texts[0])
        except json.JSONDecodeError:
            return texts[0]
    return [json.loads(t) for t in texts]
