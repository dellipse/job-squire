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
"""job-squire query -- terminal access to a running instance over MCP.

Ported from the old jobsquire-cli project's jobsquire/cli.py, with the
Hermes ~/.hermes/mcp_client.py sidecar swapped for the self-contained
client in mcp_client.py. Settled command set (docs/job-squire-cli.md):
health, list, pipeline, contacts, job, contact, followups. `stages` and
`top` from the old CLI were folded away (aliases/filters, not new
capability) and `overdue` was renamed `followups` as part of settling the
grammar in this fold-in.
"""
import json
from collections import defaultdict
from dataclasses import dataclass

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import mcp_client
from .config import QueryConfigError, load_query_config

# Mirrors ACTIVE_STATUSES in job-squire's app/models.py. job-squire is a
# separate process (talked to only over MCP), so this can't be imported
# directly -- keep it in sync by hand if that set ever changes there.
ACTIVE_STATUSES = {"Saved", "Applied", "Phone Screen", "Interview", "Final Interview", "Offer"}


@dataclass
class _QueryState:
    json_output: bool = False
    instance: str | None = None


_state = _QueryState()
console = Console()


# ---------------------------------------------------------------------------
# MCP call helper
# ---------------------------------------------------------------------------

def _call(name: str, args: dict | None = None):
    """Load config and invoke one MCP tool.

    QueryConfigError and mcp_client.MCPError are both RuntimeError
    subclasses, so callers wrapped in _safe_run get a clean one-line error
    either way without needing to distinguish the two here.
    """
    cfg = load_query_config(_state.instance)
    return mcp_client.call_tool(cfg.endpoint, cfg.token, name, args or {})


def _jobs(data):
    """Extract job list from a bare list or a dict with a 'jobs' key."""
    return data if not hasattr(data, "get") else data.get("jobs", [])


def _fmt_score(score):
    return str(score) if score is not None else "–"


def _fmt_date(val):
    return str(val)[:10] if val else "–"


def _safe_run(fn, *args, **kwargs):
    """Run a command function, printing clean errors instead of tracebacks."""
    try:
        fn(*args, **kwargs)
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)
    except Exception as e:
        console.print(f"[red]Unexpected error:[/red] {e}")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_pipeline():
    data = _call("get_pipeline")
    jobs = _jobs(data)
    stages = defaultdict(list)
    for j in jobs:
        stages[j.get("status", "Unknown")].append(j)

    if _state.json_output:
        print(json.dumps({s: len(v) for s, v in sorted(stages.items())}))
        return

    table = Table(title=f"Job Pipeline ({len(jobs)} total)", show_header=True)
    table.add_column("Stage", justify="left")
    table.add_column("Count", justify="right")
    for stage, items in sorted(stages.items()):
        table.add_row(stage, str(len(items)))
    console.print(table)


def cmd_list(stage: str = "Saved", limit: int = 20):
    pseudo = stage.strip().lower()
    # "all" and "active" aren't real status values in job-squire -- list_jobs
    # only understands an exact status match (or "" for everything), so both
    # pseudo-stages fetch everything and "active" filters client-side.
    query_status = "" if pseudo in ("all", "active") else stage
    data = _call("list_jobs", {"status": query_status})
    items = _jobs(data)
    if pseudo == "active":
        items = [j for j in items if j.get("status") in ACTIVE_STATUSES]
    total = len(items)
    shown = items[:limit]

    if _state.json_output:
        print(json.dumps(shown))
        return

    if not items:
        console.print(f"[dim]No jobs with status '{stage}'.[/dim]")
        return

    title = f"Jobs — {stage}  ({total} total"
    if total > limit:
        title += f", showing first {limit}"
    title += ")"
    table = Table(title=title)
    table.add_column("ID", justify="right")
    table.add_column("Title", justify="left")
    table.add_column("Company", justify="left")
    table.add_column("Fit", justify="right")
    table.add_column("Date", justify="left")
    for j in shown:
        table.add_row(
            str(j.get("id", "?")),
            j.get("title", "?")[:45],
            (j.get("company") or "?")[:28],
            _fmt_score(j.get("ai_fit_score")),
            _fmt_date(j.get("date_applied") or j.get("created_at")),
        )
    console.print(table)


def cmd_followups():
    data = _call("list_overdue_followups")
    jobs = _jobs(data)
    submissions = data.get("submissions", []) if hasattr(data, "get") else []

    if _state.json_output:
        print(json.dumps({"jobs": jobs, "submissions": submissions}))
        return

    if not jobs and not submissions:
        console.print("[green]No overdue follow-ups.[/green]")
        return
    if jobs:
        table = Table(title="Jobs — Overdue Follow-up")
        table.add_column("ID", justify="right")
        table.add_column("Title", justify="left")
        table.add_column("Company", justify="left")
        for j in jobs:
            table.add_row(
                str(j.get("id")),
                j.get("title", "?")[:42],
                (j.get("company") or "?")[:30],
            )
        console.print(table)
        console.print()
    if submissions:
        console.print("[bold]Overdue submissions:[/bold]")
        for s in submissions:
            contact_name = (s.get("contact") or {}).get("name", "?")
            console.print(
                f"  {contact_name} → {s.get('company', '?')}: "
                f"{s.get('role_title', '?')} [dim](since {s.get('follow_up_date', '?')})[/dim]"
            )


def cmd_contacts(ctype: str = "Recruiter"):
    data = _call("list_contacts", {"contact_type": ctype})
    items = data if not hasattr(data, "get") else data.get("contacts", [])

    if _state.json_output:
        print(json.dumps(items))
        return

    if not items:
        console.print(f"[dim]No contacts of type '{ctype}'.[/dim]")
        return

    table = Table(title=f"Contacts — {ctype}")
    table.add_column("ID", justify="right")
    table.add_column("Name", justify="left")
    table.add_column("Company", justify="left")
    table.add_column("Open Reqs", justify="right")
    table.add_column("Title", justify="left")
    for c in items:
        table.add_row(
            str(c.get("id", "?")),
            c.get("name", "?"),
            c.get("agency") or "–",
            str(c.get("open_submissions", 0)),
            (c.get("title") or "")[:40],
        )
    console.print(table)


def cmd_job_detail(job_id: int):
    data = _call("get_job", {"job_id": job_id})
    if not data:
        console.print(f"[red]Job {job_id} not found.[/red]")
        return

    if _state.json_output:
        print(json.dumps(data))
        return

    console.print(
        Panel(
            f"[bold]{data.get('title', '?')}[/bold]\n"
            f"[cyan]{data.get('company', '?')}[/cyan] — {data.get('location') or 'remote'}\n\n"
            f"Status: {data.get('status', '?')} | Fit: {_fmt_score(data.get('ai_fit_score'))}\n"
            f"Salary: {data.get('salary') or 'not listed'}\n"
            f"Source: {data.get('source', '?')} | URL: {data.get('url') or 'none'}\n\n"
            f"[dim]{data.get('notes', '')[:400]}[/dim]",
            title=f"Job #{job_id}",
            border_style="blue",
        )
    )


def cmd_contact_detail(contact_id: int):
    data = _call("get_contact", {"contact_id": contact_id})
    if not data:
        console.print(f"[red]Contact {contact_id} not found.[/red]")
        return

    if _state.json_output:
        print(json.dumps(data))
        return

    subs = data.get("submissions", [])
    console.print(
        Panel(
            f"[bold]{data.get('name', '?')}[/bold]\n"
            f"{data.get('title') or ''} @ {data.get('agency') or 'independent'}\n"
            f"Type: {data.get('type', '?')}\n"
            f"Email: {data.get('email') or '–'} | Phone: {data.get('phone') or '–'}\n"
            f"LinkedIn: {data.get('linkedin_url') or '–'}\n\n"
            f"Notes: {data.get('notes') or '–'}\n\n"
            f"Submissions ({len(subs)}):\n"
            + "\n".join(
                f"  • {s.get('company', '?')}: {s.get('role_title', '?')} "
                f"[{s.get('status', '?')}] {s.get('submitted_date', '?')}"
                for s in subs[:8]
            ),
            title=f"Contact #{contact_id}",
            border_style="green",
        )
    )


def cmd_health():
    try:
        cfg = load_query_config(_state.instance)
    except QueryConfigError as e:
        console.print(f"[red]FAIL[/red] — {e}")
        raise SystemExit(1)

    if mcp_client.check_health(cfg.endpoint):
        console.print(f"[green]Server OK[/green] — {cfg.endpoint}/health responded.")
    else:
        console.print(f"[red]FAIL[/red] — {cfg.endpoint}/health did not respond.")
        raise SystemExit(1)

    try:
        data = mcp_client.call_tool(cfg.endpoint, cfg.token, "list_jobs", {"status": "Saved"})
        items = _jobs(data)
        console.print(f"[green]MCP OK[/green] — {len(items)} saved job(s)")
    except mcp_client.MCPError as exc:
        console.print(f"[red]MCP FAIL[/red] — {exc}")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# click group
# ---------------------------------------------------------------------------

@click.group(name="query")
@click.option("--json", "json_output", is_flag=True, default=False,
              help="Output raw JSON (pipe into jq, etc.)")
@click.option("--instance", "-i", "instance", default=None,
              help="Which registered instance to query (default: the instance set with "
                   "`job-squire configure <name> --set-default`, or the sole configured one).")
def query(json_output, instance):
    """Query a running job-squire instance over MCP."""
    _state.json_output = json_output
    _state.instance = instance


@query.command()
def pipeline():
    """Full pipeline summary by stage."""
    _safe_run(cmd_pipeline)


@query.command(name="list")
@click.argument("stage", default="Saved")
@click.option("--limit", "-l", default=20, type=int, show_default=True,
              help="Max rows to show")
def list_cmd(stage, limit):
    """List jobs filtered by status."""
    _safe_run(cmd_list, stage, limit)


@query.command()
def followups():
    """Jobs and submissions with overdue follow-ups."""
    _safe_run(cmd_followups)


@query.command()
@click.argument("ctype", default="Recruiter")
def contacts(ctype):
    """List contacts by type (Recruiter, Hiring Manager, etc.)."""
    _safe_run(cmd_contacts, ctype)


@query.command()
@click.argument("job_id", type=int)
def job(job_id):
    """Full detail for one job."""
    _safe_run(cmd_job_detail, job_id)


@query.command()
@click.argument("contact_id", type=int)
def contact(contact_id):
    """Full detail for one contact."""
    _safe_run(cmd_contact_detail, contact_id)


@query.command()
def health():
    """Check job-squire MCP connectivity."""
    cmd_health()
