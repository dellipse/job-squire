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
"""job_squire_cli.ops.fileperms: owner-only permissions on every host OS.

The Windows branch is exercised here with an injected runner on every
platform; the real `icacls` path is covered on the Windows CI leg by the
backup/compose/dns tests through tests/_perms.assert_owner_only.
"""

import os
import stat
from types import SimpleNamespace

import pytest

from job_squire_cli.ops import fileperms

SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"
WHOAMI_CSV = f'"desktop-abc\\runner","{SID}"\r\n'


def scripted_run(*replies):
    """Return each reply in order; record argv. A reply that is an exception
    instance is raised instead."""
    calls = []
    queue = list(replies)

    def _run(argv, **kwargs):
        calls.append(list(argv))
        reply = queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    _run.calls = calls
    return _run


def ok(stdout=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def fail():
    return SimpleNamespace(returncode=1, stdout="", stderr="boom")


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_posix_sets_0600_without_any_subprocess(tmp_path):
    path = tmp_path / "secret.env"
    path.write_text("SECRET_KEY=x\n")
    path.chmod(0o644)
    run = scripted_run()  # any call would IndexError

    assert fileperms.restrict_to_owner(path, system="Linux", run=run) is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert run.calls == []


def test_current_user_sid_parses_whoami_csv():
    assert fileperms.current_user_sid(run=scripted_run(ok(WHOAMI_CSV))) == SID


@pytest.mark.parametrize("reply", [fail(), ok("garbage\n"), OSError("no whoami")])
def test_current_user_sid_returns_none_when_whoami_cannot_answer(reply):
    assert fileperms.current_user_sid(run=scripted_run(reply)) is None


def test_windows_replaces_acl_with_owner_and_system_only(tmp_path):
    path = tmp_path / "cloudflare.ini"
    path.write_text("dns_cloudflare_api_token = t\n")
    run = scripted_run(ok(WHOAMI_CSV), ok())

    assert fileperms.restrict_to_owner(path, system="Windows", run=run) is True
    assert run.calls[0][:2] == ["whoami", "/user"]
    assert run.calls[1] == [
        "icacls", str(path), "/inheritance:r",
        "/grant:r", f"*{SID}:F", "/grant:r", f"*{fileperms.SYSTEM_SID}:F",
    ]


def test_windows_skips_icacls_when_the_user_sid_is_unknown(tmp_path):
    path = tmp_path / "x"
    path.write_text("x")
    run = scripted_run(fail())

    assert fileperms.restrict_to_owner(path, system="Windows", run=run) is False
    assert len(run.calls) == 1  # whoami only; never an icacls with no grantee


@pytest.mark.parametrize("icacls_reply", [fail(), OSError("no icacls")])
def test_windows_reports_failure_when_icacls_fails(tmp_path, icacls_reply):
    path = tmp_path / "x"
    path.write_text("x")
    run = scripted_run(ok(WHOAMI_CSV), icacls_reply)

    assert fileperms.restrict_to_owner(path, system="Windows", run=run) is False
