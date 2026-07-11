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
"""Local-mode port pair allocation (Prompt C5, PLAN Section 4 "Port
allocation and lifecycle bookkeeping").

`create` needs a free web/MCP host port pair that doesn't collide with any
other *registered* instance (the registry is the source of truth) and is
*actually* bindable right now (a port could be in use by something the
registry doesn't know about). Both checks matter -- registry-only would
race a stale/removed-outside-the-CLI container using the same port;
socket-only would still hand out a port a stopped instance owns, which
would collide the moment that instance starts again.
"""
from __future__ import annotations

import socket
from typing import Callable, Iterable

from .registry import Instance

DEFAULT_APP_PORT = 8080  # matches docker-compose.single.yml's APP_HOST_PORT default
DEFAULT_MCP_PORT = 9000  # matches docker-compose.single.yml's MCP_HOST_PORT default
MAX_SCAN = 1000

PortFree = Callable[[int], bool]


def default_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """True if `port` can be bound on loopback right now.

    Loopback specifically, not "" (all interfaces): local-mode instances
    always publish on loopback only (PLAN Section 5), so that's the
    interface that actually matters for a collision.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
        return True


def _next_free(start: int, taken: set[int], port_free: PortFree) -> int:
    port = start
    for _ in range(MAX_SCAN):
        if port not in taken and port_free(port):
            return port
        port += 1
    raise RuntimeError(f"No free port found in {start}..{start + MAX_SCAN} -- too many instances?")


def allocate_port_pair(
    existing: Iterable[Instance],
    *,
    port_free: PortFree = default_port_free,
) -> tuple[int, int]:
    """The next free (app_port, mcp_port) pair, skipping every port already
    recorded in the registry as well as any port that isn't actually free."""
    used_app = {i.app_port for i in existing if i.app_port is not None}
    used_mcp = {i.mcp_port for i in existing if i.mcp_port is not None}
    app_port = _next_free(DEFAULT_APP_PORT, used_app, port_free)
    mcp_port = _next_free(DEFAULT_MCP_PORT, used_mcp, port_free)
    return app_port, mcp_port
