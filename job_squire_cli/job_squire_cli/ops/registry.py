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
"""The cross-platform instance registry.

A per-user, per-OS JSON file the CLI owns, recording non-secret instance
metadata as the source of truth for lifecycle (create/start/stop/status
drive against it; this module only owns the registry itself).

Lives at the same per-user config directory query.config already uses
(`config_dir()`), so the CLI has one config home per user, not one per
subsystem -- see query/config.py's docstring for the exact per-OS paths.

The `Instance` dataclass *is* the schema enforcement: it declares exactly
the fields from the plan (name, mode, runtime, data_dir, app_port,
mcp_port, cookie_name, public_url, created) and nothing else, so there is
no field a secret could be smuggled into -- passing an unexpected keyword
such as `secret_key=` to `add_instance`/`update_instance` raises
`TypeError`/`RegistryError` rather than silently writing it to disk.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

from ..query.config import config_dir

REGISTRY_FILENAME = "instances.json"
REGISTRY_VERSION = 1

_SLUG_WHITESPACE_RE = re.compile(r"[\s_]+")
_SLUG_INVALID_RE = re.compile(r"[^a-z0-9-]+")
_SLUG_REPEAT_HYPHEN_RE = re.compile(r"-{2,}")


class RegistryError(RuntimeError):
    """Raised for any registry read/write/lookup failure."""


class InvalidNameError(RegistryError):
    """Raised when an instance name has no usable slug."""


class NameCollisionError(RegistryError):
    """Raised when an instance name collides with one already registered."""


# ── Name rules and derived values ───────────────────────────────────────


def sanitize_slug(raw: str) -> str:
    """Sanitize an instance name to a safe slug: lowercase, `[a-z0-9-]`.

    Whitespace and underscores become hyphens, everything else invalid is
    dropped, repeated hyphens collapse to one, and leading/trailing
    hyphens are stripped. Raises InvalidNameError if nothing usable is
    left (e.g. an empty string or a name made entirely of punctuation).
    """
    lowered = _SLUG_WHITESPACE_RE.sub("-", raw.strip().lower())
    slug = _SLUG_INVALID_RE.sub("", lowered)
    slug = _SLUG_REPEAT_HYPHEN_RE.sub("-", slug).strip("-")
    if not slug:
        raise InvalidNameError(
            f"{raw!r} has no valid characters left after sanitizing to a slug "
            f"(lowercase letters, digits, and hyphens only). Choose a different name."
        )
    return slug


def derive_cookie_name(name: str) -> str:
    """`<name>_session` -- keeps instances on a shared hostname/localhost
    from clobbering each other's session cookies."""
    return f"{name}_session"


def derive_compose_project(name: str) -> str:
    """`job-squire-<name>` -- namespaces containers so instances don't collide."""
    return f"job-squire-{name}"


# ── Schema ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Instance:
    """One registry entry. Every field here is non-secret by construction --
    see this module's docstring. Field order matches the on-disk JSON
    shape so serialized entries stay stable and readable."""

    name: str
    mode: str
    runtime: str
    data_dir: str
    app_port: int | None
    mcp_port: int | None
    cookie_name: str
    public_url: str
    created: str


_INSTANCE_FIELD_NAMES = frozenset(f.name for f in fields(Instance))


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Storage ──────────────────────────────────────────────────────────────


def registry_path() -> Path:
    return config_dir() / REGISTRY_FILENAME


def load_registry() -> dict:
    """The raw registry dict: `{"version": ..., "instances": [...]}`.

    A missing file is not an error -- it means no instances are registered
    yet -- and returns the empty-registry shape rather than raising.
    """
    path = registry_path()
    if not path.exists():
        return {"version": REGISTRY_VERSION, "instances": []}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"Cannot read instance registry at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError(f"Instance registry at {path} is malformed (expected an object).")
    data.setdefault("version", REGISTRY_VERSION)
    data.setdefault("instances", [])
    return data


def _write_registry(data: dict) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, indent=2) + "\n")
    tmp_path.replace(path)  # atomic rename on POSIX and Windows


def list_instances() -> list[Instance]:
    return [Instance(**row) for row in load_registry()["instances"]]


def get_instance(name: str) -> Instance | None:
    for instance in list_instances():
        if instance.name == name:
            return instance
    return None


def add_instance(
    *,
    name: str,
    mode: str,
    runtime: str,
    data_dir: str,
    public_url: str,
    app_port: int | None = None,
    mcp_port: int | None = None,
    cookie_name: str | None = None,
    created: str | None = None,
) -> Instance:
    """Sanitize and register a new instance. Raises NameCollisionError if
    the sanitized name already exists. `cookie_name`/`created` are derived
    automatically when omitted; pass them explicitly only when preserving
    values from an existing record (e.g. `restore`).
    """
    slug = sanitize_slug(name)
    data = load_registry()
    if any(row["name"] == slug for row in data["instances"]):
        raise NameCollisionError(f"An instance named {slug!r} is already registered.")

    instance = Instance(
        name=slug,
        mode=mode,
        runtime=runtime,
        data_dir=str(data_dir),
        app_port=app_port,
        mcp_port=mcp_port,
        cookie_name=cookie_name or derive_cookie_name(slug),
        public_url=public_url,
        created=created or _today(),
    )
    data["instances"].append(asdict(instance))
    _write_registry(data)
    return instance


def update_instance(name: str, **changes) -> Instance:
    """Update fields of an already-registered instance and return the new
    record. Only the declared Instance fields (minus `name`, which is the
    key) may be changed -- anything else raises RegistryError, which is
    what keeps a secret from ever being written here by mistake.
    """
    unknown = set(changes) - (_INSTANCE_FIELD_NAMES - {"name"})
    if unknown:
        raise RegistryError(f"Unknown or disallowed instance field(s): {sorted(unknown)}")

    data = load_registry()
    for row in data["instances"]:
        if row["name"] == name:
            row.update(changes)
            _write_registry(data)
            return Instance(**row)
    raise RegistryError(f"No instance named {name!r} is registered.")


def remove_instance(name: str) -> bool:
    """Remove an instance from the registry. Returns False if it wasn't
    registered. Does not touch the instance's data directory or container
    -- that decision belongs to the `remove` lifecycle command (C5)."""
    data = load_registry()
    remaining = [row for row in data["instances"] if row["name"] != name]
    if len(remaining) == len(data["instances"]):
        return False
    data["instances"] = remaining
    _write_registry(data)
    return True


# ── Divergence check and reconcile ──────────────────────────────────────


@dataclass(frozen=True)
class ObservedState:
    """What's actually running for an instance, as reported by whatever
    drives the container runtime (C5). Left as a plain description rather
    than a live inspection here, since this module only owns the registry
    side of the comparison.
    """

    container_running: bool
    container_name: str | None = None
    app_port: int | None = None
    mcp_port: int | None = None
    data_dir_exists: bool | None = None


@dataclass(frozen=True)
class Drift:
    field: str
    expected: object
    actual: object

    def __str__(self) -> str:
        return f"{self.field}: expected {self.expected!r}, found {self.actual!r}"


def check_divergence(instance: Instance, observed: ObservedState) -> list[Drift]:
    """Compare the registry entry against reality and report drift:
    a renamed container, changed ports, or a deleted data directory/volume.
    """
    drifts: list[Drift] = []
    expected_project = derive_compose_project(instance.name)

    if not observed.container_running:
        drifts.append(Drift("container", expected_project, None))
    elif observed.container_name is not None and observed.container_name != expected_project:
        drifts.append(Drift("container_name", expected_project, observed.container_name))

    if observed.app_port is not None and observed.app_port != instance.app_port:
        drifts.append(Drift("app_port", instance.app_port, observed.app_port))
    if observed.mcp_port is not None and observed.mcp_port != instance.mcp_port:
        drifts.append(Drift("mcp_port", instance.mcp_port, observed.mcp_port))
    if observed.data_dir_exists is False:
        drifts.append(Drift("data_dir", instance.data_dir, "missing (directory or volume deleted)"))

    return drifts


def reconcile_instance(name: str, observed: ObservedState) -> Instance:
    """Sync the registry to observed reality for the fields that are safe
    to reconcile automatically (port drift). A renamed or missing
    container/volume is a decision for the operator, not something to
    silently rewrite, so those stay reported by `check_divergence` for a
    later CLI command (C5) to act on -- this only handles port drift.
    """
    changes = {}
    if observed.app_port is not None:
        changes["app_port"] = observed.app_port
    if observed.mcp_port is not None:
        changes["mcp_port"] = observed.mcp_port
    if not changes:
        raise RegistryError("Nothing reconcilable in the observed state (no port drift to sync).")
    return update_instance(name, **changes)
