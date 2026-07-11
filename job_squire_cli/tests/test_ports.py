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
"""Local-mode port pair allocation (Prompt C5)."""
import pytest

from job_squire_cli.ops import ports
from job_squire_cli.ops.registry import Instance


def make_instance(name, app_port, mcp_port):
    return Instance(
        name=name, mode="local", runtime="docker", data_dir=f"/tmp/{name}",
        app_port=app_port, mcp_port=mcp_port, cookie_name=f"{name}_session",
        public_url=f"http://localhost:{app_port}", created="2026-07-11",
    )


def always_free(_port: int) -> bool:
    return True


def test_allocate_defaults_when_nothing_registered():
    assert ports.allocate_port_pair([], port_free=always_free) == (
        ports.DEFAULT_APP_PORT, ports.DEFAULT_MCP_PORT,
    )


def test_allocate_skips_ports_already_in_registry():
    existing = [make_instance("first", ports.DEFAULT_APP_PORT, ports.DEFAULT_MCP_PORT)]
    app_port, mcp_port = ports.allocate_port_pair(existing, port_free=always_free)
    assert app_port == ports.DEFAULT_APP_PORT + 1
    assert mcp_port == ports.DEFAULT_MCP_PORT + 1


def test_allocate_skips_ports_that_are_not_actually_bindable():
    # Nothing registered, but the default app port is (for whatever
    # reason) not bindable right now -- something the registry doesn't
    # know about is squatting on it.
    def port_free(port):
        return port != ports.DEFAULT_APP_PORT

    app_port, mcp_port = ports.allocate_port_pair([], port_free=port_free)
    assert app_port == ports.DEFAULT_APP_PORT + 1
    assert mcp_port == ports.DEFAULT_MCP_PORT


def test_two_sequential_allocations_never_collide():
    """Simulates creating two local instances back to back: the second
    allocation must see the first instance's ports as taken."""
    existing = []
    first = ports.allocate_port_pair(existing, port_free=always_free)
    existing.append(make_instance("one", *first))
    second = ports.allocate_port_pair(existing, port_free=always_free)
    assert first != second
    assert second[0] != first[0]
    assert second[1] != first[1]


def test_allocate_raises_when_scan_exhausted():
    def never_free(_port):
        return False

    with pytest.raises(RuntimeError, match="No free port found"):
        ports.allocate_port_pair([], port_free=never_free)


def test_default_port_free_reflects_a_real_bound_socket():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        bound_port = sock.getsockname()[1]
        assert ports.default_port_free(bound_port) is False
    # Freed once the socket closes.
    assert ports.default_port_free(bound_port) is True
