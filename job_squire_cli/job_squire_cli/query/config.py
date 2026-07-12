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
"""Where the query group gets its MCP endpoint and token (Prompt C6,
docs/PROMPTS-deployment-cli.md).

The on-disk shape is keyed by instance name, because one machine can have
several registered instances (docs/PLAN-deployment-modes.md Section 4) and
each needs its own endpoint and, usually, its own bearer token:

    {
      "version": 1,
      "default": "castelo",
      "instances": {
        "castelo": {"endpoint": "http://localhost:9000", "token": "jsq_mcp_..."}
      }
    }

`ops/commands.py`'s `configure` command (Prompt C6) is the only writer:
`--mcp-token generate/rotate` calls ops/mcp_token.py to mint and store the
local static token in the instance's own database, then records the
plaintext token and derived endpoint here so the query group can use it
without a manually supplied token; `--token`/`--endpoint` let an operator
wire in an OAuth access token obtained elsewhere instead (OAuth stays the
default, untouched MCP auth flow in every mode -- nothing is generated for
it here). This file is deliberately separate from the instance registry
(ops/registry.py): the registry is non-secret metadata the CLI treats as
its source of truth for lifecycle, while this file exists specifically to
hold a secret (the bearer token), so the two are never conflated.

This module is read directly by the query group and never through a
Hermes token store, never through `~/.hermes/` at all -- see
docs/job-squire-cli.md ("Query group configuration").
"""
from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILENAME = "mcp.json"
CONFIG_VERSION = 1

ENV_ENDPOINT = "JOB_SQUIRE_MCP_URL"
ENV_TOKEN = "JOB_SQUIRE_MCP_TOKEN"


class QueryConfigError(RuntimeError):
    """Raised when no usable MCP endpoint/token can be resolved."""


@dataclass(frozen=True)
class QueryConfig:
    endpoint: str
    token: str | None
    instance: str | None = None  # None when resolved from the env-var override


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


# ── Raw storage ──────────────────────────────────────────────────────────


def _empty_config() -> dict:
    return {"version": CONFIG_VERSION, "default": None, "instances": {}}


def load_raw_config() -> dict:
    """The raw `mcp.json` dict: `{"version", "default", "instances": {name: {...}}}`.

    A missing file is not an error -- it means nothing is configured yet --
    and returns the empty-config shape rather than raising, mirroring
    ops/registry.py's load_registry().
    """
    path = config_path()
    if not path.exists():
        return _empty_config()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise QueryConfigError(f"Cannot read MCP config at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise QueryConfigError(f"MCP config at {path} is malformed (expected an object).")
    data.setdefault("version", CONFIG_VERSION)
    data.setdefault("default", None)
    data.setdefault("instances", {})
    return data


def _write_raw_config(data: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, indent=2) + "\n")
    try:
        tmp_path.chmod(0o600)  # may hold a bearer token in plaintext
    except OSError:
        pass
    tmp_path.replace(path)  # atomic rename on POSIX and Windows


# ── Per-instance entries (written by `job-squire configure`) ────────────


def set_instance(
    name: str, *, endpoint: str, token: str | None = None,
    clear_token: bool = False, make_default: bool | None = None,
) -> None:
    """Upsert one instance's endpoint/token.

    `clear_token=True` removes a stored token without touching the
    endpoint. `make_default=None` (the default) sets this instance as the
    default only if none is set yet -- the first instance ever configured
    naturally becomes the one `job-squire query` uses without `--instance`
    -- pass True/False to set or clear it explicitly.
    """
    data = load_raw_config()
    entry = dict(data["instances"].get(name, {}))
    entry["endpoint"] = endpoint.rstrip("/")
    if clear_token:
        entry.pop("token", None)
    elif token is not None:
        entry["token"] = token
    data["instances"][name] = entry

    if make_default is True:
        data["default"] = name
    elif make_default is False:
        if data.get("default") == name:
            data["default"] = None
    elif not data.get("default"):
        data["default"] = name

    _write_raw_config(data)


def clear_token(name: str) -> None:
    """Remove a stored token (revoke) without touching the endpoint or
    removing the instance entry. A no-op if the instance isn't configured."""
    data = load_raw_config()
    entry = data["instances"].get(name)
    if entry is not None and "token" in entry:
        del entry["token"]
        _write_raw_config(data)


def remove_instance_config(name: str) -> None:
    data = load_raw_config()
    if name not in data["instances"]:
        return
    del data["instances"][name]
    if data.get("default") == name:
        data["default"] = next(iter(data["instances"]), None)
    _write_raw_config(data)


# ── Resolution (read by the query group) ─────────────────────────────────


def _resolve_instance_name(data: dict, instance: str | None) -> str:
    instances = data["instances"]
    if instance:
        if instance not in instances:
            configured = ", ".join(sorted(instances)) or "(none)"
            raise QueryConfigError(
                f"No MCP config for instance {instance!r}. Configured: {configured}. "
                f"Run `job-squire configure {instance} --mcp-token generate` first."
            )
        return instance
    if data.get("default"):
        return data["default"]
    if len(instances) == 1:
        return next(iter(instances))
    if not instances:
        raise QueryConfigError(
            f"No MCP endpoint configured. Set {ENV_ENDPOINT} (and {ENV_TOKEN} if the instance "
            f"requires one) for a quick check, or run `job-squire configure <name> --mcp-token "
            f"generate` to configure one."
        )
    configured = ", ".join(sorted(instances))
    raise QueryConfigError(
        f"Multiple instances configured ({configured}) and none is the default. Pass "
        f"--instance <name>, or run `job-squire configure <name> --set-default`."
    )


def load_query_config(instance: str | None = None) -> QueryConfig:
    """Resolve the MCP endpoint and token to query against.

    Precedence: `JOB_SQUIRE_MCP_URL` / `JOB_SQUIRE_MCP_TOKEN` environment
    variables first (useful for smoke-testing and CI, and for overriding
    the file without editing it), then the per-instance entry in
    `mcp.json`, selected by `instance` if given, else the configured
    default, else the sole entry if there's exactly one. Raises
    QueryConfigError with an actionable message if nothing resolves.
    """
    env_endpoint = os.environ.get(ENV_ENDPOINT)
    if env_endpoint:
        return QueryConfig(endpoint=env_endpoint.rstrip("/"), token=os.environ.get(ENV_TOKEN), instance=None)

    data = load_raw_config()
    name = _resolve_instance_name(data, instance)
    entry = data["instances"][name]
    endpoint = entry.get("endpoint")
    if not endpoint:
        raise QueryConfigError(f"{config_path()}: instance {name!r} is missing a required 'endpoint' field.")
    return QueryConfig(endpoint=str(endpoint).rstrip("/"), token=entry.get("token"), instance=name)
