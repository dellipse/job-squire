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
"""Deployment/lifecycle command grammar.

Prompt C1 settled the full grammar and shipped every command as a
structural stub. Prompt C5 (this file, as of the commands below) makes
`create`, `start`, `stop`, `restart`, `status`, `list`, and `remove` real,
wired to ops/lifecycle.py; `update`, `configure`, `backup`, and `restore`
stay stubs until C6-C8. Every real command here is a thin click adapter:
it does the interactive prompting and prints results, and delegates every
actual decision to ops/lifecycle.py, which takes no click objects and is
directly unit-testable on its own.
"""
from __future__ import annotations

from typing import NoReturn

import click

from . import lifecycle
from .compose import DEFAULT_IMAGE
from .registry import (
    InvalidNameError,
    NameCollisionError,
    RegistryError,
    get_instance,
    list_instances,
    sanitize_slug,
)

_STUB_SPECS = [
    ("update", "Update an instance to a new image version, with rollback.", "C7"),
    ("configure", "Adjust an instance's settings, including MCP auth.", "C6"),
    ("backup", "Create a passphrase-encrypted backup archive.", "C8"),
    ("restore", "Restore an instance from a backup archive.", "C8"),
]


def _make_stub_command(name: str, summary: str, prompt: str) -> click.Command:
    help_text = (
        f"{summary}\n\n"
        f"Not implemented yet -- lands in Prompt {prompt} of "
        f"docs/PROMPTS-deployment-cli.md."
    )

    @click.command(
        name=name,
        help=help_text,
        context_settings={"ignore_unknown_options": True},
    )
    @click.argument("_args", nargs=-1, type=click.UNPROCESSED)
    def _cmd(_args):
        click.echo(
            f"job-squire {name}: not implemented yet "
            f"(Prompt {prompt} of docs/PROMPTS-deployment-cli.md).",
            err=True,
        )
        raise SystemExit(1)

    return _cmd


# ── Shared error handling ────────────────────────────────────────────────


def _fail(message: str) -> NoReturn:
    click.echo(message, err=True)
    raise SystemExit(1)


def _handle_lifecycle_error(exc: lifecycle.LifecycleError) -> NoReturn:
    if isinstance(exc, lifecycle.StartupGuardFailure):
        # Reprint the app's own FATAL reason/fix verbatim (PLAN Section 7
        # "Surfacing failures") rather than a generic "container exited".
        for line in exc.messages:
            click.echo(line, err=True)
        if not exc.messages:
            click.echo(str(exc), err=True)
    else:
        click.echo(str(exc), err=True)
    raise SystemExit(1)


# ── create ────────────────────────────────────────────────────────────────


@click.command(help="Create a new local or network instance.")
@click.argument("name", required=False)
@click.option("--mode", type=click.Choice(lifecycle.VALID_MODES), default=None,
              help="local (loopback only) or network (behind a reverse proxy).")
@click.option("--hostname", default=None, help="Public hostname (required for --mode network).")
@click.option("--mcp-hostname", default=None, help="MCP hostname (network mode; defaults to mcp-<hostname>).")
@click.option("--import-from", default=None, help="Instance name to import basic settings from.")
@click.option("--copy-keys", is_flag=True, default=False,
              help="Also copy provider/SMTP/AI API keys from --import-from (decrypted and re-encrypted).")
@click.option("--admin-username", default="admin", show_default=True)
@click.option("--admin-password", default=None, help="Defaults to a freshly generated random password.")
@click.option("--user-password", default="", help="Leave blank to create only the admin account.")
@click.option("--image", default=DEFAULT_IMAGE, show_default=True)
@click.option("--orbstack", "prefer_orbstack", is_flag=True, default=False,
              help="Prefer OrbStack over Podman if a runtime install is needed (macOS).")
@click.option("--docker-desktop", "prefer_docker_desktop", is_flag=True, default=False,
              help="Prefer Docker Desktop over Podman if a runtime install is needed (Windows).")
@click.option("--yes", "assume_yes", is_flag=True, default=False,
              help="Don't ask before installing a container runtime.")
def create(name, mode, hostname, mcp_hostname, import_from, copy_keys, admin_username, admin_password,
           user_password, image, prefer_orbstack, prefer_docker_desktop, assume_yes):
    if not name:
        name = click.prompt("Instance name")
    if not mode:
        mode = click.prompt("Deployment mode", type=click.Choice(lifecycle.VALID_MODES), default="local")
    if mode == "network" and not hostname:
        hostname = click.prompt("Public hostname (e.g. squire.example.com)")

    # Fail fast on a colliding name before asking anything else --
    # lifecycle.create_instance() would also catch this, but only after
    # the import-settings prompt below had already asked irrelevant
    # questions about an instance that's never going to be created.
    try:
        slug = sanitize_slug(name)
    except InvalidNameError as exc:
        _fail(str(exc))
    if get_instance(slug) is not None:
        _fail(f"An instance named {slug!r} is already registered.")

    if import_from is None:
        others = [i.name for i in list_instances()]
        if others and click.confirm(
            f"Import basic settings from an existing instance? ({', '.join(others)})", default=False
        ):
            import_from = click.prompt("Import from which instance", type=click.Choice(others))
            if not copy_keys:
                copy_keys = click.confirm(
                    "Also copy provider/SMTP/AI API keys? (off by default; decrypts with the source "
                    "instance's key and re-encrypts for the new one)", default=False,
                )

    confirm = (lambda _msg: True) if assume_yes else click.confirm

    try:
        result = lifecycle.create_instance(
            name=name, mode=mode, hostname=hostname, mcp_hostname=mcp_hostname,
            admin_username=admin_username, admin_password=admin_password, user_password=user_password,
            import_from=import_from, copy_keys=copy_keys, image=image, confirm=confirm,
            prefer_orbstack=prefer_orbstack, prefer_docker_desktop=prefer_docker_desktop,
        )
    except (NameCollisionError, InvalidNameError, RegistryError) as exc:
        _fail(str(exc))
    except lifecycle.LifecycleError as exc:
        _handle_lifecycle_error(exc)

    inst = result.instance
    click.echo(f"Instance {inst.name!r} created ({inst.mode} mode, runtime={inst.runtime}).")
    click.echo(f"  Web: {inst.public_url}")
    if inst.mode == "local":
        click.echo(f"  MCP: http://localhost:{inst.mcp_port}")
    else:
        click.echo(f"  MCP: https://{mcp_hostname or ('mcp-' + hostname)}")
    click.echo(f"  Admin login: {result.admin_username} / {result.admin_password}")
    if result.admin_password_generated:
        click.echo("  (password generated -- save it now, it will not be shown again)")
    if result.import_summary is not None:
        summary = result.import_summary
        if summary.tables_copied:
            click.echo(f"  Imported from {import_from!r}: {', '.join(summary.tables_copied)}")
        if summary.schedule_vars_copied:
            click.echo(f"  Imported schedule vars: {', '.join(summary.schedule_vars_copied)}")
        for warning in summary.warnings:
            click.echo(f"  Warning: {warning}")


# ── start / stop / restart ───────────────────────────────────────────────


@click.command(help="Start an existing instance's container.")
@click.argument("name")
def start(name):
    try:
        lifecycle.start_instance(name)
    except lifecycle.LifecycleError as exc:
        _handle_lifecycle_error(exc)
    click.echo(f"Instance {name!r} started.")


@click.command(help="Stop a running instance's container.")
@click.argument("name")
def stop(name):
    try:
        lifecycle.stop_instance(name)
    except lifecycle.LifecycleError as exc:
        _handle_lifecycle_error(exc)
    click.echo(f"Instance {name!r} stopped.")


@click.command(help="Restart an instance's container.")
@click.argument("name")
def restart(name):
    try:
        lifecycle.restart_instance(name)
    except lifecycle.LifecycleError as exc:
        _handle_lifecycle_error(exc)
    click.echo(f"Instance {name!r} restarted.")


# ── status / list ─────────────────────────────────────────────────────────


def _print_statuses(statuses: list[lifecycle.InstanceStatus]) -> None:
    if not statuses:
        click.echo("No instances registered. Run `job-squire create` to make one.")
        return
    click.echo(f"{'NAME':<20}{'MODE':<10}{'HEALTH':<14}{'RUNTIME':<10}{'URL'}")
    for entry in statuses:
        inst = entry.instance
        click.echo(f"{inst.name:<20}{inst.mode:<10}{entry.health:<14}{inst.runtime:<10}{inst.public_url}")
        for drift in entry.drift:
            click.echo(f"    drift: {drift}")


@click.command(name="list", help="List all registered instances (see `job-squire query list` for jobs).")
def list_instances_cmd():
    _print_statuses(lifecycle.list_status())


@click.command(help="Show health and drift for one instance, or all instances if NAME is omitted.")
@click.argument("name", required=False)
def status(name):
    if not name:
        _print_statuses(lifecycle.list_status())
        return
    instance = lifecycle.get_instance(name)
    if instance is None:
        _fail(f"No instance named {name!r} is registered.")
    _print_statuses([lifecycle.status_for(instance)])


# ── remove ────────────────────────────────────────────────────────────────


@click.command(help="Tear down an instance and update the registry.")
@click.argument("name")
@click.option("--keep-data/--delete-data", "keep_data", default=None,
              help="Skip the prompt: force keep or delete the instance's data directory.")
@click.option("--yes", "assume_yes", is_flag=True, default=False,
              help="Don't prompt; without --keep-data/--delete-data this keeps the data (the safe default).")
def remove(name, keep_data, assume_yes):
    confirm_delete = None if (keep_data is not None or assume_yes) else click.confirm
    try:
        result = lifecycle.remove_instance(name, keep_data=keep_data, confirm_delete=confirm_delete)
    except lifecycle.LifecycleError as exc:
        _handle_lifecycle_error(exc)
    click.echo(f"Instance {result.name!r} removed.")
    click.echo(f"Data directory {'kept' if result.data_kept else 'deleted'}: {result.data_dir}")


# ── registration ─────────────────────────────────────────────────────────


def register_ops_commands(group: click.Group) -> None:
    """Attach the flat deployment/lifecycle verbs directly onto `group`."""
    for command in (create, start, stop, restart, status, list_instances_cmd, remove):
        group.add_command(command)
    for name, summary, prompt in _STUB_SPECS:
        group.add_command(_make_stub_command(name, summary, prompt))
