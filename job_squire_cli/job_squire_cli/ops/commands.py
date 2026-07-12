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
structural stub. Prompt C5 made `create`, `start`, `stop`, `restart`,
`status`, `list`, and `remove` real, wired to ops/lifecycle.py. Prompt C6
(this file, as of `configure` below) makes MCP authentication real too:
`update`, `backup`, and `restore` stay stubs until C7-C8. Every real
command here is a thin click adapter: it does the interactive prompting
and prints results, and delegates every actual decision to ops/lifecycle.py
(or, for `configure`, ops/mcp_token.py and query/config.py), which take no
click objects and are directly unit-testable on their own.
"""
from __future__ import annotations

from typing import NoReturn
from urllib.parse import urlparse

import click

from . import lifecycle, mcp_token, paths, secrets_copy
from .compose import DEFAULT_IMAGE
from .registry import (
    Instance,
    InvalidNameError,
    NameCollisionError,
    RegistryError,
    get_instance,
    list_instances,
    sanitize_slug,
)
from ..query import config as query_config

_STUB_SPECS = [
    ("update", "Update an instance to a new image version, with rollback.", "C7"),
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


# ── configure (MCP authentication + query-group token-config plumbing) ────
# Prompt C6: docs/PLAN-deployment-modes.md Section 5 ("MCP authentication").
# OAuth 2.0/PKCE stays the default, untouched MCP flow in every mode --
# nothing is generated for it here. This command manages the one sanctioned
# alternative, the local `jsq_mcp_` static bearer token (loopback-only
# unless explicitly enabled on a network-reachable instance), and persists
# each instance's MCP endpoint plus a bearer token in query/config.py's
# mcp.json so `job-squire query` can use it without one supplied by hand.


def _derive_mcp_endpoint(instance: Instance) -> str:
    """Best-effort default the operator can override with --endpoint.

    Local mode: the loopback MCP port from the registry -- always accurate,
    since that's the exact port `create` published. Network mode: the
    registry only records the *web* public_url (Instance has no
    public_mcp_url/public_mcp_host field), so this falls back to the same
    `mcp-<hostname>` convention `create --mcp-hostname` defaults to
    (ops/lifecycle.py) -- accurate unless the operator picked a custom
    --mcp-hostname at creation time, in which case --endpoint corrects it.
    """
    if instance.mode == "local":
        return f"http://localhost:{instance.mcp_port or 9000}"
    hostname = urlparse(instance.public_url).hostname or instance.public_url
    return f"https://mcp-{hostname}"


def _existing_or_derived_endpoint(instance: Instance) -> str:
    entry = query_config.load_raw_config()["instances"].get(instance.name)
    if entry and entry.get("endpoint"):
        return entry["endpoint"]
    return _derive_mcp_endpoint(instance)


def _print_mcp_config(instance: Instance) -> None:
    root = paths.instance_root(instance.name)
    click.echo(f"Instance: {instance.name}  (mode={instance.mode})")
    try:
        state = mcp_token.read_state(root)
    except mcp_token.McpTokenError as exc:
        click.echo(f"  Static MCP token: unavailable -- {exc}")
        state = None
    if state is not None:
        label = "yes" if state.usable else ("expired" if state.active else "no")
        click.echo(f"  Static MCP token active: {label}")
        if state.active:
            click.echo(f"    created:    {state.created_at}")
            click.echo(f"    last used:  {state.last_used_at or '(never)'}")
            click.echo(f"    expires:    {state.expires_at or '(never, unless a TTL was set)'}")
        click.echo(f"    allow-network opt-in: {state.allow_network}")

    data = query_config.load_raw_config()
    entry = data["instances"].get(instance.name)
    click.echo(f"  Query config endpoint: {entry['endpoint'] if entry else '(not configured)'}")
    click.echo(f"  Query config token:    {'set' if entry and entry.get('token') else '(not set)'}")
    click.echo(f"  Default for `job-squire query`: {'yes' if data.get('default') == instance.name else 'no'}")
    click.echo("  OAuth 2.0/PKCE is the default MCP auth flow in every mode; the static token above")
    click.echo("  is the loopback-only escape hatch for headless clients (PLAN Section 5).")


@click.command(help="Adjust an instance's settings, including MCP authentication.")
@click.argument("name")
@click.option("--mcp-token", "mcp_token_action", type=click.Choice(["generate", "rotate", "revoke"]),
              default=None,
              help="Manage the local jsq_mcp_ static bearer token. generate/rotate both mint a "
                   "fresh token (the app only ever keeps one active); generate refuses to clobber "
                   "an existing one, rotate requires one to already exist.")
@click.option("--ttl-hours", type=float, default=None,
              help="Optional expiry for a generated/rotated token (default: no expiry).")
@click.option("--allow-network/--no-allow-network", "allow_network", default=None,
              help="Explicit opt-in (or withdrawal) to allow the static token on a network-"
                   "reachable instance. Required alongside --mcp-token generate/rotate there; "
                   "never enabled implicitly.")
@click.option("--token", "manual_token", default=None,
              help="Manually set the query group's stored bearer token, e.g. an OAuth access "
                   "token obtained elsewhere. Alternative to --mcp-token.")
@click.option("--endpoint", "manual_endpoint", default=None,
              help="Override the stored MCP endpoint (default: derived from the instance's "
                   "registry entry).")
@click.option("--set-default/--no-set-default", "set_default", default=None,
              help="Make this instance the query group's default (or clear it as default).")
@click.option("--show", "show_only", is_flag=True, default=False,
              help="Print the instance's current MCP auth configuration and exit.")
def configure(name, mcp_token_action, ttl_hours, allow_network, manual_token, manual_endpoint,
              set_default, show_only):
    instance = get_instance(name)
    if instance is None:
        _fail(f"No instance named {name!r} is registered.")

    if mcp_token_action is not None and manual_token is not None:
        _fail("Choose either --mcp-token or --token, not both.")

    no_op = (
        show_only or (mcp_token_action is None and manual_token is None and manual_endpoint is None
                       and set_default is None and allow_network is None)
    )
    if no_op:
        _print_mcp_config(instance)
        return

    root = paths.instance_root(instance.name)

    if allow_network is not None and mcp_token_action is None:
        try:
            mcp_token.set_allow_network(root, allow_network)
        except mcp_token.McpTokenError as exc:
            _fail(str(exc))
        click.echo(
            f"Static token network opt-in for {instance.name!r}: "
            f"{'enabled' if allow_network else 'disabled'}."
        )

    if mcp_token_action is not None:
        try:
            state = mcp_token.read_state(root)
        except mcp_token.McpTokenError as exc:
            _fail(str(exc))

        if mcp_token_action == "generate" and state.usable:
            _fail(
                f"Instance {instance.name!r} already has an active MCP token. "
                f"Use --mcp-token rotate to replace it, or revoke first."
            )
        if mcp_token_action == "rotate" and not state.usable:
            _fail(f"No active MCP token for {instance.name!r} yet. Use --mcp-token generate.")

        if mcp_token_action == "revoke":
            mcp_token.revoke(root)
            if manual_endpoint is not None or set_default is not None:
                query_config.set_instance(
                    instance.name, endpoint=(manual_endpoint or _existing_or_derived_endpoint(instance)),
                    clear_token=True, make_default=set_default,
                )
            else:
                query_config.clear_token(instance.name)
            click.echo(f"MCP token revoked for {instance.name!r}.")
            return

        effective_allow_network = allow_network if allow_network is not None else state.allow_network
        if not mcp_token.is_static_token_allowed(instance.mode, effective_allow_network):
            _fail(
                f"Instance {instance.name!r} is network-reachable (mode=network). The static "
                f"token is refused there unless explicitly enabled -- pass --allow-network to "
                f"opt in, or prefer OAuth (the default flow) for a reachable instance."
            )
        if allow_network is not None:
            try:
                mcp_token.set_allow_network(root, allow_network)
            except mcp_token.McpTokenError as exc:
                _fail(str(exc))

        try:
            secret_key = secrets_copy.read_secret_key(root)
        except secrets_copy.SecretsCopyError as exc:
            _fail(str(exc))

        token = mcp_token.write_new_token(root, secret_key, ttl_hours=ttl_hours)
        endpoint = manual_endpoint or _derive_mcp_endpoint(instance)
        query_config.set_instance(instance.name, endpoint=endpoint, token=token, make_default=set_default)

        click.echo(f"MCP token {mcp_token_action}d for {instance.name!r}: {token}")
        click.echo(
            f"  (save it now -- this is the only time it's shown; it's stored for "
            f"`job-squire query` at {query_config.config_path()})"
        )
        if ttl_hours and ttl_hours > 0:
            click.echo(f"  Expires in {ttl_hours} hour(s).")
        return

    # No --mcp-token action: manual endpoint/token/default-only adjustments.
    if manual_token is not None or manual_endpoint is not None or set_default is not None:
        endpoint = manual_endpoint or _existing_or_derived_endpoint(instance)
        query_config.set_instance(
            instance.name, endpoint=endpoint, token=manual_token, make_default=set_default,
        )
        parts = [f"endpoint={endpoint}"]
        if manual_token is not None:
            parts.append("token set")
        if set_default is True:
            parts.append("now default")
        elif set_default is False:
            parts.append("no longer default")
        click.echo(f"Updated MCP config for {instance.name!r}: {', '.join(parts)}.")


# ── registration ─────────────────────────────────────────────────────────


def register_ops_commands(group: click.Group) -> None:
    """Attach the flat deployment/lifecycle verbs directly onto `group`."""
    for command in (create, start, stop, restart, status, list_instances_cmd, remove, configure):
        group.add_command(command)
    for name, summary, prompt in _STUB_SPECS:
        group.add_command(_make_stub_command(name, summary, prompt))
