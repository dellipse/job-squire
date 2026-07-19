# job-squire-cli

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%203.0-blue.svg)](LICENSE.md)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)

The `job-squire` command line tool: the deployment front door for a
[job-squire](https://github.com/dellipse/job-squire) instance, and a query
client for checking your pipeline, contacts, and follow-ups from the
terminal without opening a browser.

This package folds in what used to be the separate `jobsquire-cli` project.
See `docs/job-squire-cli.md` in the `job-squire` repo for the settled
command grammar and the versioning rule.

## Two command groups

- **Deployment/lifecycle** (top-level): `create`, `start`, `stop`,
  `restart`, `status`, `list`, `update`, `remove`, `configure`, `backup`,
  `restore`. These drive an instance's container lifecycle. See
  `docs/job-squire-cli.md` in the `job-squire` repo for the full command
  grammar.
- **Query** (`job-squire query ...`): `health`, `list`, `pipeline`,
  `contacts`, `job`, `contact`, `followups`. Talks to a running instance's
  MCP server directly over the standard Streamable HTTP transport -- no
  Hermes, no `~/.hermes/` sidecar, nothing vendored from any other project.

## Installation

```bash
pip install /path/to/job-squire/job_squire_cli          # deployment group only
pip install "/path/to/job-squire/job_squire_cli[query]" # + the query group
```

This installs both `job-squire` (canonical) and `jobsquire` (alias, for
existing muscle memory) into the environment.

## Query group configuration

The query group reads its target instance's MCP endpoint and token from:

1. `JOB_SQUIRE_MCP_URL` / `JOB_SQUIRE_MCP_TOKEN` environment variables, or
2. a small JSON config file at the conventional per-user config location
   (`~/.config/job-squire/mcp.json` on Linux, `~/Library/Application
   Support/job-squire/mcp.json` on macOS, `%APPDATA%\job-squire\mcp.json`
   on Windows), shaped `{"endpoint": "http://localhost:9000", "token":
   "jsq_mcp_..."}`.

`job-squire configure` will write that file for you once it lands; until
then, set the environment variables directly.
