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
made MCP authentication real too (`configure`). Prompt C7 made version
movement real (`update`). Prompt C8 (this file, as of `backup`/`restore`
below) makes passphrase-encrypted backup and restore real, wired to
ops/backup.py. (Prompt C7 also shipped an `adopt` command for migrating an
existing three-container install onto the single-container image; removed
2026-07-17 once no installs remained on the old topology to migrate.)
Every real command here is a thin click
adapter: it does the interactive prompting and prints results, and
delegates every actual decision to ops/lifecycle.py (or, for `configure`,
ops/mcp_token.py and query/config.py; for `backup`/`restore`,
ops/backup.py), which take no click objects and are directly
unit-testable on their own.
"""
from __future__ import annotations

from pathlib import Path
from typing import NoReturn
from urllib.parse import urlparse

import click

from . import backup, compose, lifecycle, mcp_token, secrets_copy
from . import self_update
from . import dns as dns_ops
from . import ollama_assist
from . import proxy as proxy_ops
from . import tailscale as tailscale_ops
from . import uninstall as uninstall_ops
from .compose import DEFAULT_IMAGE
from .registry import (
    Instance,
    InvalidNameError,
    NameCollisionError,
    RegistryError,
    derive_compose_project,
    get_instance,
    list_instances,
    sanitize_slug,
)
from ..query import config as query_config


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
              help="Don't ask before installing a container runtime or setting up Ollama.")
@click.option("--skip-ollama-check", is_flag=True, default=False,
              help="Don't check this machine's local-AI (Ollama) capability after the instance comes up.")
def create(name, mode, hostname, mcp_hostname, import_from, copy_keys, admin_username, admin_password,
           user_password, image, prefer_orbstack, prefer_docker_desktop, assume_yes, skip_ollama_check):
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

    if not skip_ollama_check:
        _offer_ollama_setup(inst, confirm=confirm)


def _offer_ollama_setup(instance: Instance, *, confirm) -> None:
    """Automatic tail of `create`: once the instance's container is up,
    check this host's local-AI capability (ops/ollama_assist.py) and, if
    the machine can reasonably run local models, offer to install Ollama
    (if missing) or configure it for this instance (if already installed).
    Mirrors `job-squire ollama check`/`setup` above, but triggered
    automatically as part of bootstrap rather than requiring the operator
    to know those subcommands exist -- see docs/PLAN-ollama-assist.md.

    Best-effort only: any failure here is reported and swallowed rather
    than raised, so a detection/install/configure hiccup never undoes an
    otherwise-successful `create`. `confirm` is the same callable `create`
    already built from `--yes` (`assume_yes`), so `--yes` skips this
    prompt too, same as it does for the container runtime.
    """
    try:
        caps = ollama_assist.detect_host_capabilities()
    except Exception as exc:  # pragma: no cover - detection is normally infallible; defensive only
        click.echo(f"\n(Skipped local-AI check: {exc})")
        return

    rec = ollama_assist.recommend(caps)
    if rec is None:
        # This machine can't run local models well -- say nothing further;
        # the operator didn't ask about Ollama and can't use it here anyway.
        return

    click.echo(f"\nThis machine can run local AI models via Ollama (tier: {rec.tier}).")
    click.echo(f"  {rec.description}")

    if caps.ollama_installed:
        prompt = (
            f"Ollama is already installed -- configure {instance.name!r} to use it now? "
            f"(pulls ~{rec.approx_download_gb:.0f} GB of models)"
        )
    else:
        prompt = (
            f"Install Ollama and configure {instance.name!r} to use it for local, private AI "
            f"analysis? (installs Ollama, pulls ~{rec.approx_download_gb:.0f} GB of models)"
        )
    if not confirm(prompt):
        click.echo(f"  Skipped. Run `job-squire ollama setup {instance.name}` later if you change your mind.")
        return

    try:
        result = ollama_assist.run_setup(
            Path(instance.data_dir), runtime=instance.runtime,
            container_name=derive_compose_project(instance.name),
            confirm=lambda _msg: True,  # consent already captured above
        )
    except ollama_assist.OllamaAssistError as exc:
        click.echo(f"  Ollama setup failed: {exc}")
        click.echo(f"  Re-run later with `job-squire ollama setup {instance.name}`.")
        return

    if result.provider_configured:
        click.echo(f"  Configured Ollama provider for {instance.name!r}: base_url={result.base_url}")
    if result.automatic_features_enabled:
        click.echo("  Enabled Automatic AI Features (auto-triage/follow-up drafts/weekly review can now run).")
    if result.roundtrip_ok is True:
        click.echo(f"  Round-trip test: ok (model replied: {result.roundtrip_detail!r})")
    elif result.roundtrip_ok is False:
        click.echo(f"  Round-trip test: FAILED -- {result.roundtrip_detail}")


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


# ── update / rollback (Prompt C7) ────────────────────────────────────────


def _run_self_update(skip: bool, cli_version: str | None) -> None:
    """Bring the CLI itself up to date before any instance is touched
    (see ops/self_update.py). A failed self-update is a warning, not a
    fatal error -- an operator offline, or hitting a flaky GitHub API,
    should still be able to move an already-pulled image onto a running
    instance; --skip-self-update opts out of even trying."""
    if skip:
        return
    try:
        result = self_update.self_update(cli_version)
    except self_update.SelfUpdateError as exc:
        click.echo(f"Warning: could not update the job-squire CLI itself: {exc}", err=True)
        return
    if result.updated:
        click.echo(f"job-squire CLI updated: {result.previous_version} -> {result.new_version} ({result.tag})")
    else:
        click.echo(f"job-squire CLI already up to date ({result.previous_version}, {result.tag}).")


@click.command(help="Update the CLI itself, then an instance to a new image version "
                     "(or roll back to its previous one).")
@click.argument("name", required=False)
@click.option("--all", "all_instances", is_flag=True, default=False,
              help="Update every registered instance instead of one NAME.")
@click.option("--version", default=None,
              help="Instance image tag to move to (default: latest). Accepts a bare tag or a full image ref.")
@click.option("--rollback", "do_rollback", is_flag=True, default=False,
              help="Move instance(s) back to the image they were running before their last update, "
                   "instead of moving forward. Cannot be combined with --version.")
@click.option("--skip-self-update", is_flag=True, default=False,
              help="Don't update the job-squire CLI itself first -- only move instance(s).")
@click.option("--cli-version", default=None,
              help="Pin the CLI self-update to this released version instead of the latest (ignored "
                   "with --skip-self-update).")
def update(name, all_instances, version, do_rollback, skip_self_update, cli_version):
    if do_rollback and version is not None:
        _fail("Choose either --version or --rollback, not both.")
    if all_instances and name:
        _fail("Choose either an instance NAME or --all, not both.")

    _run_self_update(skip_self_update, cli_version)

    if not all_instances and not name:
        return  # bare `job-squire update`: self-update only, no instance to move.

    # Real instance existence is validated inside lifecycle.update_instance/
    # rollback_instance itself (InstanceNotFoundError), same as before this
    # command grew --all -- no separate registry lookup here for the
    # single-NAME case, so a caller that's mocked lifecycle.update_instance
    # directly (as the tests below do) doesn't need the registry populated.
    target_names = [inst.name for inst in list_instances()] if all_instances else [name]
    if all_instances and not target_names:
        _fail("No instances are registered -- nothing to update.")

    verb = "rolled back" if do_rollback else "updated"
    for target_name in target_names:
        try:
            if do_rollback:
                result = lifecycle.rollback_instance(target_name)
            else:
                result = lifecycle.update_instance(target_name, version=version or "latest")
        except lifecycle.LifecycleError as exc:
            _handle_lifecycle_error(exc)
        click.echo(f"Instance {target_name!r} {verb}: {result.previous_image} -> {result.new_image}")
        if result.health is not None:
            health_status = result.health.get("Health", {}).get("Status") or result.health.get("Status")
            click.echo(f"  Health: {health_status}")


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
@click.option("--remove-image/--keep-image", "remove_image", default=False, show_default=True,
              help="Also remove the instance's container image with 'rmi' -- but only if no other "
                   "registered instance still references it. 'compose down' alone never removes "
                   "the image it was running.")
@click.option("--yes", "assume_yes", is_flag=True, default=False,
              help="Don't prompt; without --keep-data/--delete-data this keeps the data (the safe default).")
def remove(name, keep_data, remove_image, assume_yes):
    confirm_delete = None if (keep_data is not None or assume_yes) else click.confirm
    try:
        result = lifecycle.remove_instance(
            name, keep_data=keep_data, confirm_delete=confirm_delete, remove_image=remove_image,
        )
    except lifecycle.LifecycleError as exc:
        _handle_lifecycle_error(exc)
    click.echo(f"Instance {result.name!r} removed.")
    click.echo(f"Data directory {'kept' if result.data_kept else 'deleted'}: {result.data_dir}")
    if remove_image:
        if result.image_removed:
            click.echo(f"Image removed: {result.image}")
        elif result.image:
            click.echo(f"Image kept ({result.image}): {result.image_kept_reason}")


# ── uninstall ────────────────────────────────────────────────────────────
# Removes every registered instance and, by default, each instance's
# container image (unlike `remove`, where image cleanup stays opt-in --
# see ops/uninstall.py's module docstring), then (opt-in) the container
# runtime job-squire itself installed, then the CLI's own venv and PATH
# entry. Not part of the original C1-C12 grammar; added because getting
# job-squire *off* a machine matters as much as getting it on, and the
# bootstrap scripts already modify PATH and (via `create`) may have
# installed a runtime, so the CLI should be able to fully reverse both, on
# request.


@click.command(help="Uninstall job-squire: remove every registered instance, optionally the "
                     "container runtime it installed, and the CLI itself.")
@click.option("--keep-data/--delete-data", "keep_data", default=None,
              help="Skip the per-instance prompt: force keep or delete every instance's data "
                   "directory (database, uploads, SECRET_KEY).")
@click.option("--remove-runtime/--keep-runtime", "remove_runtime", default=False, show_default=True,
              help="Also uninstall the container runtime (Podman/OrbStack/Docker Desktop) -- but "
                   "only if job-squire installed it itself; a runtime that was already working on "
                   "this machine before job-squire is never touched.")
@click.option("--remove-image/--keep-image", "remove_image", default=None,
              help="Remove each instance's container image with 'rmi' once nothing else "
                   "references it. This is the default for uninstall (unlike 'remove', which "
                   "leaves images alone unless asked) -- 'compose down' alone never removes the "
                   "image it was running, and an uninstall is normally a full teardown. Without "
                   "either flag, you're asked whether to keep the image instead; the prompt "
                   "defaults to No (remove).")
@click.option("--yes", "assume_yes", is_flag=True, default=False,
              help="Don't prompt; without --keep-data/--delete-data this keeps every instance's "
                   "data (the safe default), without --remove-runtime the runtime is never "
                   "removed even if job-squire installed it, and without --keep-image every "
                   "instance's image is removed (the default for uninstall).")
def uninstall(keep_data, remove_runtime, remove_image, assume_yes):
    instances = list_instances()
    if instances:
        click.echo("This removes every registered instance: " + ", ".join(i.name for i in instances))
    else:
        click.echo("No instances are registered.")

    # This is the one prompt --yes can't skip past silently: it gates the
    # whole operation, not just a single instance's data. Defaults to "no"
    # -- pressing Enter must never uninstall anything -- and --yes is the
    # explicit, opt-in way to bypass it for scripted use.
    if not assume_yes and not click.confirm(
        "Uninstall job-squire? This tears down every registered instance's container "
        "(data is kept by default -- see --delete-data), and also removes the CLI itself "
        "and its PATH entry if this looks like a bootstrap.sh/.ps1 install.",
        default=False,
    ):
        click.echo("Aborted -- nothing was uninstalled.")
        return

    confirm_delete = None if (keep_data is not None or assume_yes) else click.confirm
    confirm_runtime = None if assume_yes else click.confirm

    # Unlike keep_data/remove_runtime, an uninstall's default *is* to remove
    # the image -- --remove-image/--keep-image on the command line wins
    # outright; otherwise --yes proceeds with that same default (remove);
    # otherwise ask, defaulting the prompt itself to "No" (don't keep, i.e.
    # remove) so pressing Enter matches the stated default.
    if remove_image is None:
        remove_image = True if assume_yes else not click.confirm(
            "Keep the container image(s) instead of removing them?", default=False,
        )

    try:
        result = uninstall_ops.uninstall_everything(
            keep_data=keep_data, confirm_delete_data=confirm_delete,
            remove_runtime=remove_runtime, confirm_runtime=confirm_runtime,
            remove_image=remove_image,
        )
    except lifecycle.LifecycleError as exc:
        _handle_lifecycle_error(exc)
    except uninstall_ops.UninstallError as exc:
        _fail(str(exc))

    if remove_image:
        for name in result.instances_removed:
            line = f"  {name}: data {'kept' if result.data_kept[name] else 'deleted'}"
            if result.image_removed.get(name):
                line += ", image removed"
            else:
                reason = result.image_kept_reason.get(name)
                line += f", image kept{f' ({reason})' if reason else ''}"
            click.echo(line)
    else:
        for name in result.instances_removed:
            click.echo(f"  {name}: data {'kept' if result.data_kept[name] else 'deleted'}, image kept")

    if result.runtime_removed:
        click.echo(f"Runtime removed: {result.runtime_removed}")
    elif remove_runtime:
        click.echo("Runtime not removed (job-squire didn't install it, or removal was declined).")
    else:
        click.echo("Runtime left in place (pass --remove-runtime to also uninstall it).")

    if result.cli_removed:
        click.echo(f"job-squire CLI removed from {result.cli_removed}")
        if result.rc_files_updated:
            click.echo("  PATH entry removed from: " + ", ".join(str(p) for p in result.rc_files_updated))
            click.echo("Open a new terminal for the PATH change to take effect.")
        else:
            click.echo(
                "  No PATH entry was found to remove in ~/.zshrc, ~/.bashrc, or ~/.profile -- "
                "if a job-squire line is still there, remove it by hand."
            )
    else:
        click.echo(
            "job-squire's own files weren't removed automatically (this doesn't look like a "
            "bootstrap.sh/.ps1 install). If you installed it with pip, remove it with:\n"
            "    pip uninstall job-squire-cli"
        )


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
    # Path(instance.data_dir), not paths.instance_root(instance.name): the
    # registry's own recorded value is the source of truth for where an
    # instance actually lives, rather than re-deriving the default path and
    # assuming it matches.
    root = Path(instance.data_dir)
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

    # Path(instance.data_dir), not paths.instance_root(instance.name): the
    # registry's own recorded value is the source of truth for where an
    # instance actually lives, rather than re-deriving the default path and
    # assuming it matches.
    root = Path(instance.data_dir)

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
        # Instance.mode alone can't see Tailscale reachability -- Prompt C11
        # deliberately keeps a Serve-fronted instance's mode at "local" (see
        # ops/tailscale.py's module docstring), so its own state manifest is
        # consulted too. Either way, the same explicit --allow-network opt-in
        # from C6 is what's required; nothing here is allowed implicitly.
        tailnet_reachable = instance.mode == "local" and tailscale_ops.is_tailnet_reachable(root)
        if not mcp_token.is_static_token_allowed(instance.mode, effective_allow_network) or (
            tailnet_reachable and not effective_allow_network
        ):
            reachability = (
                "network-reachable (mode=network)" if instance.mode == "network"
                else "reachable over your tailnet (Tailscale Serve is enabled -- "
                     "see `job-squire tailscale status " + instance.name + "`)"
            )
            _fail(
                f"Instance {instance.name!r} is {reachability}. The static "
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


# ── backup / restore (Prompt C8) ─────────────────────────────────────────
# docs/PLAN-deployment-modes.md Section 7 ("Backup and restore"). Every
# archive is mandatory-encrypted (ops/backup_crypto.py, Argon2id +
# AES-256-GCM) -- this command layer only ever handles the passphrase as a
# hidden-input prompt (or an explicit --passphrase for scripted use, which
# the help text warns against on a shared shell history) and never prints
# or logs it.


def _require_instance(name: str) -> Instance:
    instance = get_instance(name)
    if instance is None:
        _fail(f"No instance named {name!r} is registered.")
    return instance


@click.command(name="backup", help="Create a passphrase-encrypted backup archive of an instance.")
@click.argument("name", required=False)
@click.option("--all", "all_instances", is_flag=True, default=False,
              help="Back up every registered instance instead of one NAME.")
@click.option("--dest", "dest_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Directory to write the archive(s) into (default: your home folder).")
@click.option("--format", "archive_format", type=click.Choice(["tgz", "zip"]), default="tgz", show_default=True)
@click.option("--passphrase", default=None,
              help="Backup passphrase. Omit to be prompted (recommended -- avoids leaving it in shell history).")
def backup_cmd(name, all_instances, dest_dir, archive_format, passphrase):
    if all_instances and name:
        _fail("Choose either an instance NAME or --all, not both.")
    if not all_instances and not name:
        _fail("Specify an instance NAME, or pass --all to back up every registered instance.")

    targets = list_instances() if all_instances else [_require_instance(name)]
    if not targets:
        _fail("No instances are registered -- nothing to back up.")

    if passphrase is None:
        passphrase = click.prompt("Backup passphrase", hide_input=True, confirmation_prompt=True)
    click.echo(
        "This archive is encrypted with the passphrase above and cannot be restored without it "
        "-- there is no recovery. Write it down somewhere safe; a lost passphrase means a lost backup."
    )

    for instance in targets:
        try:
            result = backup.create_backup(instance, dest_dir=dest_dir, passphrase=passphrase, ext=archive_format)
        except lifecycle.LifecycleError as exc:
            _handle_lifecycle_error(exc)
        click.echo(f"Instance {instance.name!r} backed up to {result.archive_path}")


@click.command(name="restore", help="Restore an instance from a passphrase-encrypted backup archive.")
@click.argument("archive_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--rename-to", default=None, help="Register the restored instance under a different name.")
@click.option("--overwrite", is_flag=True, default=False,
              help="Replace an existing instance of the same (or --rename-to) name instead of prompting.")
@click.option("--passphrase", default=None,
              help="Backup passphrase. Omit to be prompted (recommended -- avoids leaving it in shell history).")
@click.option("--image", default=None,
              help="Bring the restored instance up on this image instead of the one recorded in the backup.")
@click.option("--up/--no-up", "bring_up", default=True, show_default=True,
              help="Bring the restored instance up immediately after registering it.")
@click.option("--yes", "assume_yes", is_flag=True, default=False,
              help="Don't ask before installing a container runtime.")
def restore_cmd(archive_path, rename_to, overwrite, passphrase, image, bring_up, assume_yes):
    if passphrase is None:
        passphrase = click.prompt("Backup passphrase", hide_input=True)

    try:
        opened = backup.open_backup(archive_path, passphrase)
    except backup.WrongPassphraseError:
        _fail("Wrong passphrase (or the archive is corrupted) -- could not decrypt the backup.")
    except backup.RestoreError as exc:
        _fail(str(exc))

    target_name = rename_to
    if not overwrite:
        candidate = target_name or opened.instance_name
        try:
            candidate_slug = sanitize_slug(candidate)
        except InvalidNameError as exc:
            _fail(str(exc))
        if get_instance(candidate_slug) is not None:
            click.echo(f"An instance named {candidate_slug!r} is already registered.")
            choice = click.prompt(
                "Rename the restored instance, overwrite the existing one, or abort?",
                type=click.Choice(["rename", "overwrite", "abort"]), default="rename",
            )
            if choice == "abort":
                _fail("Restore cancelled.")
            elif choice == "overwrite":
                overwrite = True
            else:
                target_name = click.prompt("New instance name")

    confirm = (lambda _msg: True) if assume_yes else click.confirm

    try:
        result = backup.restore_instance(
            opened, target_name=target_name, overwrite=overwrite, image=image, bring_up=bring_up, confirm=confirm,
        )
    except (NameCollisionError, InvalidNameError, RegistryError) as exc:
        _fail(str(exc))
    except lifecycle.LifecycleError as exc:
        _handle_lifecycle_error(exc)

    inst = result.instance
    click.echo(f"Instance {inst.name!r} restored from {archive_path} ({inst.mode} mode, runtime={inst.runtime}).")
    click.echo(f"  Data dir: {result.data_dir}")
    click.echo(f"  Web: {inst.public_url}")
    if result.health is not None:
        click.echo(f"  Health: {result.health.get('Health', {}).get('Status') or result.health.get('Status')}")
    elif bring_up:
        click.echo("  Instance registered but did not come up cleanly -- see the error above.")
    else:
        click.echo(f"  Not brought up yet. Run `job-squire start {inst.name}` when you're ready.")


# ── proxy (Prompt C9) ────────────────────────────────────────────────────
# docs/PLAN-deployment-modes.md Section 5 ("Optional proxy provisioning").
# For a network-mode instance: generate its web/MCP nginx confs into an
# existing SWAG/nginx proxy and reload it, or -- if none is running --
# install a LinuxServer SWAG container first. Every actual decision lives
# in ops/proxy.py; this is a thin adapter, same as every other command here.


@click.command(name="proxy", help="Provision a reverse proxy (existing SWAG/nginx, or install SWAG) "
                                   "for a network-mode instance.")
@click.argument("name")
@click.option("--container", "proxy_container", default=None,
              help="Name of an existing proxy container to use, instead of auto-detecting one by "
                   "name/image (e.g. a SWAG container not named 'swag').")
@click.option("--config-dir", "config_dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Manually specify the proxy's host config directory instead of auto-detecting one "
                   "(needed for a bare, non-containerized nginx install).")
@click.option("--network", default=proxy_ops.DEFAULT_PROXY_NETWORK, show_default=True,
              help="Shared Docker network the instance and a containerized proxy join for name resolution.")
@click.option("--no-install", "no_install", is_flag=True, default=False,
              help="Fail instead of installing SWAG if no existing reverse proxy is detected.")
@click.option("--timezone", "swag_timezone", default="Etc/UTC", show_default=True,
              help="TZ for a freshly installed SWAG container.")
@click.option("--url", "swag_url", default="",
              help="SWAG URL env var, if a fresh SWAG install is needed. Defaults to the instance's "
                   "own hostname (SWAG cannot finish booting with no URL at all). DNS/TLS validation "
                   "is `job-squire`'s own separate DNS/TLS setup, so this doesn't need to resolve yet.")
@click.option("--validation", "swag_validation", type=click.Choice(["http", "dns"]), default="http",
              show_default=True, help="SWAG VALIDATION env var, if a fresh SWAG install is needed.")
@click.option("--yes", "assume_yes", is_flag=True, default=False,
              help="Don't ask before installing SWAG if no reverse proxy is detected.")
def proxy_cmd(name, proxy_container, config_dir, network, no_install, swag_timezone, swag_url,
              swag_validation, assume_yes):
    instance = _require_instance(name)
    if instance.mode != "network":
        _fail(
            f"Instance {name!r} is in {instance.mode!r} mode -- reverse-proxy provisioning only "
            f"applies to network-mode instances (local modes use loopback only)."
        )

    confirm = (lambda _msg: True) if assume_yes else click.confirm
    # Path(instance.data_dir), not paths.instance_root(instance.name): same
    # reason as _print_mcp_config above -- an adopted instance's data_dir
    # doesn't necessarily live under the default per-user data root.
    root = Path(instance.data_dir)

    try:
        result = proxy_ops.provision_instance_proxy(
            instance, root=root, proxy_container=proxy_container, config_dir=config_dir,
            network=network, install_if_missing=not no_install, swag_timezone=swag_timezone,
            swag_url=swag_url, swag_validation=swag_validation, confirm=confirm,
        )
    except proxy_ops.ProxyError as exc:
        _fail(str(exc))

    click.echo(f"Reverse proxy provisioned for {instance.name!r} ({result.proxy.kind}).")
    if result.installed_swag:
        click.echo(f"  Installed a new SWAG container (config at {result.proxy.config_dir}).")
        click.echo(
            "  DNS/TLS validation isn't fully configured yet -- network mode is not considered "
            "configured without a working proxy in front of it."
        )
    click.echo(f"  Shared network: {result.network}")
    click.echo(f"  Web conf installed: {result.web_conf_path}")
    click.echo(f"  MCP conf installed: {result.mcp_conf_path}")
    click.echo("  Proxy reloaded.")


# ── dns (Prompt C10) ─────────────────────────────────────────────────────
# docs/PLAN-deployment-modes.md Section 5 ("Free and low-cost domain and DNS
# options for personal use") and Section 7's DNS and TLS provisioning
# touchpoint. Both subcommands only ever configure the CLI's own SWAG
# install from `job-squire proxy NAME` (ops/dns.py's `_managed_swag_target`
# enforces this) -- everything else (Cloudflare Tunnel, other SWAG DNS
# plugins) is documented in docs/job-squire-cli.md, never wired here.
# `NAME` identifies the network-mode instance whose proxy is being
# configured, purely to reuse its recorded `runtime` the same way
# `proxy_cmd` does; SWAG itself is shared across every instance on that
# proxy, so this can be re-run for a different instance without redoing
# anything instance-specific.


@click.group(name="dns", help="Configure DNS/TLS validation for a network-mode instance's "
                               "CLI-installed SWAG proxy (DuckDNS automated, Cloudflare DNS-01 "
                               "semi-automated; run `job-squire proxy NAME` first).")
def dns_group():
    pass


def _require_network_instance(name: str) -> Instance:
    instance = _require_instance(name)
    if instance.mode != "network":
        _fail(
            f"Instance {name!r} is in {instance.mode!r} mode -- DNS/TLS provisioning only applies "
            f"to network-mode instances (local modes use loopback only and need no proxy)."
        )
    return instance


def _print_dns_result(result: dns_ops.DnsProvisionResult, *, runtime: str) -> None:
    click.echo(f"SWAG reconfigured for {result.mode} ({result.url}, SUBDOMAINS={result.subdomains!r}).")
    if result.cert.issued:
        click.echo("  Certificate issued -- the instance should now serve HTTPS through the proxy.")
    else:
        click.echo(
            "  Certificate not yet confirmed issued (or --no-wait was passed). Check "
            f"`{compose.runtime_binary(runtime)} logs {result.proxy.container_name}` for progress; "
            "DNS propagation and Let's Encrypt rate limits can both add delay."
        )


@dns_group.command(name="duckdns", help="Put the CLI-installed SWAG into DuckDNS validation mode "
                                         "and wait for Let's Encrypt to issue the certificate.")
@click.argument("name")
@click.option("--subdomain", required=True,
              help="Your registered DuckDNS name, e.g. 'castelo' for castelo.duckdns.org.")
@click.option("--token", prompt=True, hide_input=True, help="Your DuckDNS account token (from duckdns.org).")
@click.option("--wildcard/--main-only", default=True, show_default=True,
              help="Wildcard cert via DNS-01 (no inbound port needed) vs. the main subdomain only via "
                   "HTTP-01 (needs port 80 reachable from the internet). DuckDNS supports one or the "
                   "other from one SWAG config, never both at once.")
@click.option("--network", default=proxy_ops.DEFAULT_PROXY_NETWORK, show_default=True)
@click.option("--timezone", default="Etc/UTC", show_default=True)
@click.option("--no-wait", "no_wait", is_flag=True, default=False,
              help="Apply the configuration and return immediately instead of polling for the certificate.")
@click.option("--timeout", "timeout_seconds", default=300.0, show_default=True,
              help="Seconds to wait for the certificate before giving up (with --no-wait, ignored).")
def dns_duckdns_cmd(name, subdomain, token, wildcard, network, timezone, no_wait, timeout_seconds):
    instance = _require_network_instance(name)
    try:
        result = dns_ops.configure_duckdns(
            subdomain=subdomain, token=token, wildcard=wildcard, runtime=instance.runtime,
            network=network, timezone=timezone, wait_for_cert=not no_wait, timeout_seconds=timeout_seconds,
        )
    except dns_ops.DnsError as exc:
        _fail(str(exc))
    _print_dns_result(result, runtime=instance.runtime)


@dns_group.command(name="cloudflare", help="Write the CLI-installed SWAG's Cloudflare DNS-01 "
                                            "configuration and issue a wildcard certificate.")
@click.argument("name")
@click.option("--domain", required=True, help="A domain you already own, managed on Cloudflare "
                                               "(e.g. 'example.com').")
@click.option("--token", "api_token", prompt=True, hide_input=True,
              help="A Cloudflare API token scoped to Zone:DNS:Edit for --domain.")
@click.option("--network", default=proxy_ops.DEFAULT_PROXY_NETWORK, show_default=True)
@click.option("--timezone", default="Etc/UTC", show_default=True)
@click.option("--no-wait", "no_wait", is_flag=True, default=False,
              help="Apply the configuration and return immediately instead of polling for the certificate.")
@click.option("--timeout", "timeout_seconds", default=300.0, show_default=True,
              help="Seconds to wait for the certificate before giving up (with --no-wait, ignored).")
def dns_cloudflare_cmd(name, domain, api_token, network, timezone, no_wait, timeout_seconds):
    instance = _require_network_instance(name)
    try:
        result = dns_ops.configure_cloudflare(
            domain=domain, api_token=api_token, runtime=instance.runtime,
            network=network, timezone=timezone, wait_for_cert=not no_wait, timeout_seconds=timeout_seconds,
        )
    except dns_ops.DnsError as exc:
        _fail(str(exc))
    _print_dns_result(result, runtime=instance.runtime)


# ── tailscale (Prompt C11) ───────────────────────────────────────────────
# docs/PLAN-deployment-modes.md Section 5 ("Reaching a local instance from
# your own devices (Tailscale)"). Serve, never Funnel, in front of a
# *local*-mode instance's loopback ports -- see ops/tailscale.py's module
# docstring for why Instance.mode stays "local" throughout and where the
# on/off state actually lives (a per-instance tailscale.json manifest, not
# the registry).


@click.group(name="tailscale", help="Front a local instance with Tailscale Serve for private "
                                     "remote access (never Funnel; the app stays on loopback).")
def tailscale_group():
    pass


@tailscale_group.command(name="enable", help="Turn on Tailscale Serve for a local instance's "
                                              "web and MCP ports.")
@click.argument("name")
@click.option("--web-port", type=click.Choice([str(p) for p in tailscale_ops.ALLOWED_SERVE_PORTS]),
              default=str(tailscale_ops.DEFAULT_WEB_SERVE_PORT), show_default=True,
              help="Tailnet HTTPS port Serve publishes the web app on.")
@click.option("--mcp-port", type=click.Choice([str(p) for p in tailscale_ops.ALLOWED_SERVE_PORTS]),
              default=str(tailscale_ops.DEFAULT_MCP_SERVE_PORT), show_default=True,
              help="Tailnet HTTPS port Serve publishes the MCP endpoint on.")
def tailscale_enable_cmd(name, web_port, mcp_port):
    instance = _require_instance(name)
    # Path(instance.data_dir), not paths.instance_root(instance.name): same
    # reason as _print_mcp_config above -- an adopted instance's data_dir
    # doesn't necessarily live under the default per-user data root.
    root = Path(instance.data_dir)
    try:
        result = tailscale_ops.enable_tailscale_serve(
            instance, root=root, web_port=int(web_port), mcp_port=int(mcp_port),
        )
    except tailscale_ops.TailscaleError as exc:
        _fail(str(exc))

    click.echo(f"Tailscale Serve enabled for {instance.name!r}.")
    click.echo(f"  Web: {result.public_url}")
    click.echo(f"  MCP: {result.public_mcp_url}")
    if result.health is not None:
        click.echo(f"  Health: {result.health.get('Health', {}).get('Status') or result.health.get('Status')}")
    click.echo(f"  Note: {result.expected_warning}")
    click.echo(
        "  MCP over the tailnet: prefer OAuth (the default flow) now that this instance is "
        "reachable beyond this machine. The local static token still works but is refused unless "
        "explicitly opted in -- see `job-squire configure " + instance.name + " --allow-network`."
    )


@tailscale_group.command(name="disable", help="Turn off Tailscale Serve for an instance and "
                                               "revert it to loopback-only.")
@click.argument("name")
def tailscale_disable_cmd(name):
    instance = _require_instance(name)
    root = Path(instance.data_dir)
    try:
        result = tailscale_ops.disable_tailscale_serve(instance, root=root)
    except tailscale_ops.TailscaleError as exc:
        _fail(str(exc))

    click.echo(f"Tailscale Serve disabled for {instance.name!r}.")
    click.echo(f"  Web: {result.public_url}")
    if result.health is not None:
        click.echo(f"  Health: {result.health.get('Health', {}).get('Status') or result.health.get('Status')}")


@tailscale_group.command(name="status", help="Show whether Tailscale Serve is enabled for an instance.")
@click.argument("name")
def tailscale_status_cmd(name):
    instance = _require_instance(name)
    root = Path(instance.data_dir)
    try:
        state = tailscale_ops.read_state(root)
    except tailscale_ops.TailscaleError as exc:
        _fail(str(exc))

    if not state.enabled:
        click.echo(f"Tailscale Serve is not enabled for {instance.name!r}.")
        return
    click.echo(f"Tailscale Serve is enabled for {instance.name!r} (since {state.enabled_at}).")
    click.echo(f"  Hostname: {state.hostname}")
    click.echo(f"  Web port: {state.web_port}")
    click.echo(f"  MCP port: {state.mcp_port}")


# ── ollama (docs/PLAN-ollama-assist.md) ─────────────────────────────────
# CLI side of "PLAN: Ollama Assist -- Capability Detection, Guided Install,
# Model Selection" (agreed 2026-07-12). `check` is the host-detection half
# (Section "Container Blindness" -- authoritative because it runs on the
# host, not inside the app's container); `setup` is the "CLI (automation
# with consent)" install flow. Every real decision lives in
# ops/ollama_assist.py; these are thin adapters, same as every other
# command in this file.


@click.group(name="ollama", help="Detect this machine's capacity for local AI via Ollama, and "
                                  "install/configure a recommended model for an instance.")
def ollama_group():
    pass


def _print_capabilities(caps: ollama_assist.HostCapabilities) -> None:
    click.echo(f"Detected at: {caps.detected_at}  (source: {caps.source})")
    click.echo(f"  OS: {caps.os}" + (" (Apple Silicon)" if caps.apple_silicon else ""))
    click.echo(f"  RAM: {caps.ram_gb} GB" if caps.ram_gb is not None else "  RAM: could not detect")
    click.echo(
        f"  CPU cores: {caps.cpu_cores}" if caps.cpu_cores is not None else "  CPU cores: could not detect"
    )
    if caps.gpu_vendor:
        vram = f", {caps.gpu_vram_gb} GB VRAM" if caps.gpu_vram_gb is not None else ""
        click.echo(f"  GPU: {caps.gpu_vendor}{vram}")
    else:
        click.echo("  GPU: none detected")
    click.echo(f"  Ollama installed: {'yes' if caps.ollama_installed else 'no'}")
    click.echo(f"  Ollama running: {'yes' if caps.ollama_running else 'no'}")


@ollama_group.command(name="check", help="Detect this machine's RAM/CPU/GPU and report which local-AI "
                                          "tier it falls into, with recommended models. Pass NAME to also "
                                          "write the result into that instance's data dir for the web app.")
@click.argument("name", required=False)
def ollama_check_cmd(name):
    caps = ollama_assist.detect_host_capabilities()
    _print_capabilities(caps)

    rec = ollama_assist.recommend(caps)
    click.echo()
    if rec is None:
        click.echo(ollama_assist.not_reasonable_message(caps))
    else:
        click.echo(f"Tier: {rec.tier}")
        click.echo(f"  {rec.description}")
        click.echo(f"  Recommended triage model:   {rec.triage_model}")
        click.echo(f"  Recommended analysis model: {rec.analysis_model}")
        click.echo(f"  Approx. combined download:  ~{rec.approx_download_gb:.0f} GB")

    if name:
        instance = _require_instance(name)
        # Path(instance.data_dir), not paths.instance_root(instance.name): same
        # reason as _print_mcp_config above.
        root = Path(instance.data_dir)
        path = ollama_assist.write_host_capabilities(root, caps)
        click.echo(f"\nWrote {path}")


@ollama_group.command(name="setup", help="Install Ollama if needed, pull the recommended models for this "
                                          "machine, and write the Ollama provider row into NAME's database. "
                                          "Review every step first with --dry-run.")
@click.argument("name")
@click.option("--base-url", default=None,
              help="Where NAME's container reaches Ollama. Default: "
                   f"{ollama_assist.OLLAMA_CONTAINER_HOST!r} (Ollama running natively on this same host -- "
                   "the compose file maps host.docker.internal for exactly this, on every platform). "
                   "'localhost' is never correct here -- that resolves to the container itself, not this "
                   "host. Only override this if Ollama is running on a different machine on your network.")
@click.option("--triage-model", default=None, help="Override the tier's recommended triage model (base tag, "
                                                    "e.g. 'qwen3:8b' -- the context-sized derived model is "
                                                    "computed from this, not pulled/used directly).")
@click.option("--analysis-model", default=None, help="Override the tier's recommended analysis model (base tag).")
@click.option("--num-ctx", type=int, default=None,
              help="Override the tier's recommended context window (tokens) baked into the derived model. "
                   "Default: the tier's own recommendation (8192 or 16384).")
@click.option("--rank", type=int, default=None,
              help="Provider chain rank (default: keep the existing row's rank, or append after the last).")
@click.option("--skip-pull", is_flag=True, default=False, help="Don't pull models (e.g. already pulled).")
@click.option("--skip-derive", is_flag=True, default=False,
              help="Don't create a context-sized derived model -- write the base tags as-is. Ollama's "
                   "OpenAI-compatible endpoint then uses its own default context window (2048 tokens), "
                   "which app/ai.py's capacity check will treat as unconfigured (no truncation guard).")
@click.option("--skip-test", is_flag=True, default=False, help="Don't run the end-to-end round-trip test.")
@click.option("--skip-enable-features", is_flag=True, default=False,
              help="Don't turn on the app's 'Automatic Features' toggle (ai_config.api_enabled). By default "
                   "setup enables it, so auto-triage/follow-up drafts/weekly review start running against "
                   "this provider chain immediately -- pass this to configure Ollama for manual/MCP-only "
                   "use instead, leaving that toggle exactly as it was.")
@click.option("--dry-run", is_flag=True, default=False, help="Print every step without changing anything.")
@click.option("--yes", "assume_yes", is_flag=True, default=False, help="Don't ask before installing Ollama.")
def ollama_setup_cmd(name, base_url, triage_model, analysis_model, num_ctx, rank, skip_pull, skip_derive,
                      skip_test, skip_enable_features, dry_run, assume_yes):
    instance = _require_instance(name)
    # Path(instance.data_dir), not paths.instance_root(instance.name): same
    # reason as _print_mcp_config above.
    root = Path(instance.data_dir)
    confirm = (lambda _msg: True) if assume_yes else click.confirm

    try:
        result = ollama_assist.run_setup(
            root, runtime=instance.runtime, container_name=derive_compose_project(instance.name),
            base_url=base_url, triage_model=triage_model, analysis_model=analysis_model,
            num_ctx=num_ctx, rank=rank, enable_automatic_features=not skip_enable_features,
            confirm=confirm, dry_run=dry_run, skip_pull=skip_pull,
            skip_derive=skip_derive, skip_test=skip_test,
        )
    except ollama_assist.OllamaAssistError as exc:
        _fail(str(exc))

    click.echo()
    _print_capabilities(result.capabilities)
    if result.recommendation is None:
        return  # run_setup already raised for "not reasonable" -- unreachable, kept defensive.
    click.echo(f"Tier: {result.tier}")

    if dry_run:
        click.echo("\nDry run only -- nothing was installed, pulled, or written.")
        return

    if result.host_capabilities_path:
        click.echo(f"\nWrote {result.host_capabilities_path}")
    if result.models_pulled:
        click.echo(f"Pulled: {', '.join(result.models_pulled)}")
    elif skip_pull:
        click.echo("Skipped pulling models (--skip-pull).")
    if result.models_derived:
        for base_tag, derived in result.models_derived.items():
            click.echo(f"Created {derived} (num_ctx={result.num_ctx}, from {base_tag})")
    elif skip_derive:
        click.echo(
            "Skipped creating context-sized models (--skip-derive) -- Ollama's own default context "
            "window (2048 tokens) applies; app/ai.py has no configured num_ctx to check prompts against."
        )
    if result.provider_configured:
        click.echo(f"Configured Ollama provider for {instance.name!r}: base_url={result.base_url}")
    if skip_enable_features:
        click.echo("Skipped enabling Automatic AI Features (--skip-enable-features).")
    elif result.automatic_features_enabled:
        click.echo("Enabled Automatic AI Features (auto-triage/follow-up drafts/weekly review can now run).")
    elif result.provider_configured:
        click.echo(
            "Warning: could not enable Automatic AI Features -- no ai_config row found yet. Run "
            "`job-squire start NAME` at least once, then re-run this command, or turn it on by hand "
            "in Settings."
        )
    if skip_test:
        click.echo("Skipped the round-trip test (--skip-test).")
    elif result.roundtrip_ok is True:
        click.echo(f"Round-trip test: ok (model replied: {result.roundtrip_detail!r})")
    elif result.roundtrip_ok is False:
        click.echo(f"Round-trip test: FAILED -- {result.roundtrip_detail}")


# ── registration ─────────────────────────────────────────────────────────


def register_ops_commands(group: click.Group) -> None:
    """Attach the flat deployment/lifecycle verbs directly onto `group`."""
    for command in (
        create, start, stop, restart, update, status, list_instances_cmd, remove, uninstall, configure,
        backup_cmd, restore_cmd, proxy_cmd,
    ):
        group.add_command(command)
    group.add_command(dns_group)
    group.add_command(tailscale_group)
    group.add_command(ollama_group)
