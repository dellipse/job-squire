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
"""Deployment/lifecycle command grammar -- structural placeholders only.

Prompt C1 (docs/PROMPTS-deployment-cli.md) settles the command grammar and
package structure before any lifecycle behavior is built; the actual
behavior for each of these lands in the prompts named in its help text
(C4/C5 for the registry and lifecycle core, C6 for MCP auth, C7 for update
and adopt, C8 for backup/restore). Every command here accepts arbitrary
arguments and flags without error and reports that it isn't implemented
yet, so the grammar is real and discoverable via --help today without
pretending to do work it can't do.
"""
import click

_SPECS = [
    ("create", "Create a new local or network instance.", "C5"),
    ("start", "Start an existing instance's container.", "C5"),
    ("stop", "Stop a running instance's container.", "C5"),
    ("restart", "Restart an instance's container.", "C5"),
    ("status", "Show health and drift for one or all instances.", "C5"),
    ("list", "List all registered instances (see `job-squire query list` for jobs).", "C4/C5"),
    ("update", "Update an instance to a new image version, with rollback.", "C7"),
    ("remove", "Tear down an instance and update the registry.", "C5"),
    ("configure", "Adjust an instance's settings, including MCP auth.", "C6"),
    ("backup", "Create a passphrase-encrypted backup archive.", "C8"),
    ("restore", "Restore an instance from a backup archive.", "C8"),
]


def _make_stub_command(name: str, summary: str, prompt: str) -> click.Command:
    help_text = (
        f"{summary}\n\n"
        f"Not implemented yet -- lands in Prompt {prompt} of "
        f"docs/PROMPTS-deployment-cli.md. This release only settles the "
        f"command grammar (Prompt C1)."
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


def register_ops_commands(group: click.Group) -> None:
    """Attach the flat deployment/lifecycle verbs directly onto `group`."""
    for name, summary, prompt in _SPECS:
        group.add_command(_make_stub_command(name, summary, prompt))
