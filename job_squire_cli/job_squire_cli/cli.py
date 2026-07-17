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
"""job-squire -- deployment front door + MCP query client, one entry point.

Command grammar (settled in docs/job-squire-cli.md):

  job-squire create|start|stop|restart|status|list|update|remove|
             uninstall|configure|backup|restore
                                                -- deployment/lifecycle group,
                                                   flat at the top level.
  job-squire dns duckdns|cloudflare            -- DNS/TLS group.
  job-squire tailscale enable|disable|status   -- Tailscale Serve group.
  job-squire ollama check|setup                -- local-AI capability detection
                                                   and guided Ollama install
                                                   (docs/PLAN-ollama-assist.md).
  job-squire query health|list|pipeline|contacts|job|contact|followups
                                                -- query group, namespaced so
                                                   its own `list` (jobs) never
                                                   collides with the
                                                   deployment group's `list`
                                                   (instances).

`jobsquire` is wired as an alias to this same entry point (see pyproject.toml
[project.scripts]) so usage from the old jobsquire-cli project keeps working.

The query group is imported lazily (see _LazyGroup below): a deployment-only
install of this package never pays for `rich` or `mcp`, and the ops commands
above never require either to be installed or a live MCP endpoint to exist.
"""
import importlib

import click

from . import __version__
from .ops.commands import register_ops_commands


class _LazyGroup(click.Group):
    """A click.Group whose commands aren't imported until actually invoked.

    Overrides list_commands()/get_command() so subcommand dispatch doesn't
    need the load, and get_params() so this group's own options (`--json`,
    `--instance` on the real `query` group) are picked up too -- Click's
    `Command.parse_args`/`format_options` both go through `get_params(ctx)`,
    not the `params` list set at construction time, so without this
    override the wrapper parsed as if it had no group-level options at all
    (a real bug: `job-squire query --instance NAME health` failed with
    "No such option '--instance'", and `--help` silently omitted every
    group-level option). Loading here only happens once `query` itself has
    already been dispatched to from the top-level group, so a plain
    `job-squire create ...` (or `job-squire --help`, which never calls
    get_params on subcommands it merely lists) still never imports `rich`/
    `mcp` for an ops-only install.
    """

    def __init__(self, *args, import_name: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._import_name = import_name
        self._loaded = None

    def _load(self) -> click.Group:
        if self._loaded is None:
            module_name, attr = self._import_name.split(":", 1)
            try:
                module = importlib.import_module(module_name)
            except ImportError as exc:
                raise click.ClickException(
                    f"The '{self.name}' command group needs the [query] extra "
                    f"(missing: {exc.name}). Install it with:\n\n"
                    f"    pip install \"job-squire-cli[query]\"\n"
                ) from exc
            self._loaded = getattr(module, attr)
        return self._loaded

    def list_commands(self, ctx):
        return self._load().list_commands(ctx)

    def get_command(self, ctx, name):
        return self._load().get_command(ctx, name)

    def get_params(self, ctx):
        self.params = self._load().params
        return super().get_params(ctx)


query_group = _LazyGroup(
    name="query",
    import_name="job_squire_cli.query.commands:query",
    help="Query a running job-squire instance over MCP "
         "(health, list, pipeline, contacts, job, contact, followups). "
         "Requires the [query] extra.",
)


@click.group()
@click.version_option(version=__version__, prog_name="job-squire")
def main():
    """job-squire -- create, run, and query a Job Squire instance."""


register_ops_commands(main)
main.add_command(query_group)


if __name__ == "__main__":
    main()
