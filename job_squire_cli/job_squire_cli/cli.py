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
             adopt|configure|backup|restore    -- deployment/lifecycle group,
                                                   flat at the top level.
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

    Click's dispatch machinery only calls list_commands()/get_command() to
    resolve a subcommand and then invokes the returned Command object
    directly, so overriding just those two is enough -- this group's own
    `invoke` is never reached for real subcommand calls, only for
    `--help` on the group itself, which is handled by the static `help=`
    text passed at construction and doesn't need the load.
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
