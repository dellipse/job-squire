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
"""Shared "background thread + poll" infrastructure for long-running AI
calls, split out of app/main.py (QUAL-01, Long-term audit item 17).

This isn't one domain's private helper -- _TaskStatus/_StatusLogHandler and
the poll routes below are used by the jobs blueprint (ats-gap, score-fit,
draft-followup, prep-interview), the kits blueprint (kit_run, kit_batch),
the ai_tasks blueprint (analyze, run_task, triage_batch), and
app/onboarding.py's resume interview -- hence its own module rather than
living inside any one of those.
"""
import json
import logging
import os
import re

from flask import Blueprint, current_app, jsonify, render_template, request
from flask_login import login_required
from werkzeug.utils import secure_filename

task_status_bp = Blueprint("task_status", __name__)

# run_id is always server-generated via uuid.uuid4().hex (see _TaskStatus /
# the callers that create one) -- 32 lowercase hex characters, nothing else.
# The regex check below is inlined into each route right before its
# filesystem use rather than factored into a shared helper, so CodeQL's
# taint-tracking sees the sanitizing guard in the same function as the sink
# it protects (os.path.join/open/os.unlink) instead of losing the flow
# across a function boundary.
_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class _TaskStatus:
    """File-backed status object shared between the background thread and the poll endpoint.

    Written to DATA_DIR/task_{run_id}.json so it's visible across gunicorn workers.
    Writes are atomic (write-tmp + os.replace).
    """
    def __init__(self, run_id: str, task: str, data_dir: str):
        self.run_id = run_id
        self.task = task
        self.path = os.path.join(data_dir, f"task_{run_id}.json")
        self._data = {"run_id": run_id, "task": task, "status": "running",
                      "logs": [], "result": None, "error": None}
        self._flush()

    def log(self, text: str) -> None:
        self._data["logs"].append(text)
        self._flush()

    def done(self, result) -> None:
        self._data["status"] = "done"
        self._data["result"] = result
        self._flush()

    def fail(self, error) -> None:
        self._data["status"] = "error"
        self._data["error"] = str(error)
        self._flush()

    def _flush(self) -> None:
        import tempfile
        dir_ = os.path.dirname(self.path)
        try:
            fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f)
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001
            pass


class _StatusLogHandler(logging.Handler):
    """Forwards log records from the 'ai' logger into a _TaskStatus object."""
    def __init__(self, status: _TaskStatus):
        super().__init__()
        self.status = status
        self.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.status.log(self.format(record))
        except Exception:  # noqa: BLE001
            pass


@task_status_bp.route("/ai/task/<run_id>/status")
@login_required
def ai_task_status(run_id: str):
    """Status page for a running background AI task. Opened in a new tab."""
    if not _RUN_ID_RE.fullmatch(run_id or ""):
        return jsonify({"status": "not_found"}), 404
    task = request.args.get("task", "")
    labels = {
        "triage": "Auto-Triage", "followup": "Follow-Up Drafts", "weekly_review": "Weekly Review",
        "ats_gap": "ATS Gap Analysis", "score_fit": "Score Fit", "draft_followup": "Draft Follow-Up",
        "prep_interview": "Interview Prep", "analyze": "AI Analysis",
    }
    label = labels.get(task, task.replace("_", " ").title())
    return render_template("task_status.html", run_id=run_id, task=task, label=label)


@task_status_bp.route("/ai/task/<run_id>/poll")
@login_required
def ai_task_poll(run_id: str):
    """JSON endpoint polled by the status page every 2 s."""
    if not _RUN_ID_RE.fullmatch(run_id or ""):
        return jsonify({"status": "not_found"}), 404
    data_dir = current_app.config["DATA_DIR"]
    # secure_filename() is this codebase's existing sanitizer for
    # user-influenced filenames elsewhere (see the upload/kit routes above);
    # used here too so the same recognized library call guards this sink,
    # on top of the regex check above which already makes run_id safe.
    path = os.path.join(data_dir, secure_filename(f"task_{run_id}.json"))
    if not os.path.exists(path):
        return jsonify({"status": "not_found"}), 404
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return jsonify({"status": "not_found"}), 404
    # Clean up completed task files after delivering the final state.
    if data.get("status") in ("done", "error"):
        try:
            os.unlink(path)
        except OSError:
            pass
    return jsonify(data)
