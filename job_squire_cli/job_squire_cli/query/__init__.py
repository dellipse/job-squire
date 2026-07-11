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
"""The query command group -- talks to a running job-squire over MCP.

Requires the [query] extra (rich, mcp). Nothing in this package imports
~/.hermes/ or any Hermes code; it speaks the standard MCP Streamable HTTP
protocol directly via the `mcp` library the job-squire server itself
depends on.
"""
