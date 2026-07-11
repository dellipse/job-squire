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
"""Where the query group gets its MCP endpoint and token.

This is deliberately minimal. Prompt C6 (docs/PROMPTS-deployment-cli.md)
builds the CLI's real per-user token-config plumbing -- generate/rotate/
revoke via `job-squire configure`, multi-instance support, the loopback-
reachability rule -- and this module's on-disk shape is expected to grow
to match it. What's settled now, and won't change under C6, is *where* the
config lives (the same per-user config directory the instance registry
from PLAN-deployment-modes.md Section 4 uses) and that it is read directly
by this module -- never through a Hermes token store, never through
`~/.hermes/` at all.
"""
import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILENAME = "mcp.json"

ENV_ENDPOINT = "JOB_SQUIRE_MCP_URL"
ENV_TOKEN = "JOB_SQUIRE_MCP_TOKEN"


class QueryConfigError(RuntimeError):
    """Raised when no usable MCP endpoint/token can be resolved."""


@dataclass(frozen=True)
class QueryConfig:
    endpoint: str
    token: str | None


def config_dir() -> Path:
    """The per-user, per-OS config directory job-squire owns.

    Matches PLAN-deployment-modes.md Section 4's registry location so the
    CLI has exactly one config home per user, not one per subsystem:
      macOS:   ~/Library/Application Support/job-squire/
      Linux:   ~/.config/job-squire/ (honoring XDG_CONFIG_HOME)
      Windows: %APPDATA%\\job-squire\\
    """
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "job-squire"
    if system == "Windows":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / "job-squire"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg) if xdg else Path.home() / ".config"
    return root / "job-squire"


def config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def load_query_config() -> QueryConfig:
    """Resolve the MCP endpoint and token to query against.

    Precedence: JOB_SQUIRE_MCP_URL / JOB_SQUIRE_MCP_TOKEN env vars first
    (useful for smoke-testing and CI, and for overriding the file without
    editing it), then the on-disk config file. Raises QueryConfigError with
    an actionable message if neither resolves.
    """
    env_endpoint = os.environ.get(ENV_ENDPOINT)
    if env_endpoint:
        return QueryConfig(endpoint=env_endpoint.rstrip("/"), token=os.environ.get(ENV_TOKEN))

    path = config_path()
    if not path.exists():
        raise QueryConfigError(
            f"No MCP endpoint configured. Set {ENV_ENDPOINT} (and {ENV_TOKEN} "
            f"if the instance requires one) for a quick check, or run "
            f"`job-squire configure` once that command is available to write "
            f"{path}."
        )

    try:
        raw = path.read_text()
    except OSError as exc:
        raise QueryConfigError(f"Cannot read MCP config at {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise QueryConfigError(f"MCP config at {path} is not valid JSON: {exc}") from exc

    endpoint = data.get("endpoint") if isinstance(data, dict) else None
    if not endpoint:
        raise QueryConfigError(f"{path} is missing a required 'endpoint' field.")

    return QueryConfig(endpoint=str(endpoint).rstrip("/"), token=data.get("token"))
