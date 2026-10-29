#!/usr/bin/env python3
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
"""Build-time guard: fail if a distrusted root CA is still in a trust store.

Run by the Dockerfile after `pip install`. Checks two bundles:

- certifi's cacert.pem, which `requests` uses for every outbound AI-provider,
  job-board and web-search call. A hit here fails the build: certifi is
  pinned in requirements.txt, so a hit means that pin regressed.
- The system bundle (/etc/ssl/certs/ca-certificates.crt, Alpine's
  ca-certificates-bundle), which Python's ssl.create_default_context() uses
  for SMTP in app/notify.py. With --strip-system, distrusted roots found here
  are removed in place instead of failing the build, so a release isn't
  blocked waiting on Alpine to ship its own ca-certificates update. The
  bundle is then re-checked.

Why this exists: in October 2026 eight eMudhra intermediate CA keys were
found to be recoverable (weak key generation). Browsers blocked the
intermediates via OneCRL/CRLSet, which Python and OpenSSL never consult, so
for a server-side client the only protection is removing the roots those
intermediates chain to. Mozilla removed emSign ECC Root CA - G3, Root CA - C1
and ECC Root CA - C3; emSign Root CA - G1 is deliberately NOT listed below,
matching Mozilla's current decision to keep it. Revisit if that changes.

Matching is by SHA-256 fingerprint of the DER bytes, using only the stdlib,
so no certificate in either bundle ever has to be parsed.

Usage:
    python check_distrusted_roots.py [--strip-system] [--system-bundle PATH]
                                     [--certifi-bundle PATH]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import sys

DISTRUSTED = {
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b":
        "emSign ECC Root CA - G3 (eMudhra, Oct 2026 key compromise)",
    "125609aa301da0a249b97a8239cb6a34216f44dcac9f3954b14292f2e8c8608f":
        "emSign Root CA - C1 (eMudhra, Oct 2026 key compromise)",
    "bc4d809b15189d78db3e1d8cf4f9726a795da1643ca5f1358e1ddb0edc0d7eb3":
        "emSign ECC Root CA - C3 (eMudhra, Oct 2026 key compromise)",
}

SYSTEM_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

_PEM_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----\s*(.+?)\s*-----END CERTIFICATE-----\s*",
    re.DOTALL,
)


def _fingerprint(b64_body: str) -> str:
    der = base64.b64decode("".join(b64_body.split()))
    return hashlib.sha256(der).hexdigest()


def find_distrusted(pem_text: str) -> list[str]:
    """Return the labels of distrusted roots present in a PEM bundle."""
    return [DISTRUSTED[fp] for fp in
            (_fingerprint(m.group(1)) for m in _PEM_RE.finditer(pem_text))
            if fp in DISTRUSTED]


def strip_distrusted(pem_text: str) -> tuple[str, list[str]]:
    """Remove distrusted certificates from a PEM bundle, keeping the rest verbatim."""
    removed: list[str] = []

    def _repl(m):
        fp = _fingerprint(m.group(1))
        if fp in DISTRUSTED:
            removed.append(DISTRUSTED[fp])
            return ""
        return m.group(0)

    return _PEM_RE.sub(_repl, pem_text), removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strip-system", action="store_true",
                    help="remove distrusted roots from the system bundle instead of failing")
    ap.add_argument("--system-bundle", default=SYSTEM_BUNDLE)
    ap.add_argument("--certifi-bundle", default=None,
                    help="defaults to certifi.where()")
    args = ap.parse_args(argv)

    certifi_path = args.certifi_bundle
    if certifi_path is None:
        import certifi
        certifi_path = certifi.where()

    failed = False

    with open(certifi_path, encoding="ascii") as f:
        hits = find_distrusted(f.read())
    for label in hits:
        print(f"FAIL: certifi bundle {certifi_path} still trusts {label}", file=sys.stderr)
    failed |= bool(hits)

    with open(args.system_bundle, encoding="ascii") as f:
        system_text = f.read()
    if args.strip_system:
        system_text, removed = strip_distrusted(system_text)
        if removed:
            with open(args.system_bundle, "w", encoding="ascii") as f:
                f.write(system_text)
            for label in removed:
                print(f"removed from {args.system_bundle}: {label}")
    hits = find_distrusted(system_text)
    for label in hits:
        print(f"FAIL: system bundle {args.system_bundle} still trusts {label}", file=sys.stderr)
    failed |= bool(hits)

    if not failed:
        print("OK: no distrusted root CAs in the certifi or system bundles")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
