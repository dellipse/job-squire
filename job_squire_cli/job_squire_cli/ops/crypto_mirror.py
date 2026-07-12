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
"""The HKDF-SHA256 -> Fernet derivation, mirrored byte-for-byte from
app/crypto.py.

Shared by ops/secrets_copy.py (Prompt C5, re-encrypting settings copied
between instances) and ops/mcp_token.py (Prompt C6, writing the encrypted
MCP static token directly into an instance's database) so this
compatibility contract -- what the app can decrypt -- lives in exactly one
place inside this package, rather than being copied a second time. It is
mirrored rather than imported from app/crypto.py because this package
intentionally does not depend on Flask/SQLAlchemy/the app package at all
(an operator running the CLI has not necessarily cloned the app repo, and
the app's stack is meant to live inside the container, not on the host).

The values below, not the code, are the actual compatibility contract --
see tests/test_secrets_copy.py's test_fernet_derivation_matches_app_crypto,
which loads the real app/crypto.py directly and round-trips both ways.
"""
from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Must stay byte-for-byte identical to app/crypto.py's _PREFIX/_HKDF_INFO.
ENC_PREFIX = "enc:"
HKDF_INFO = b"job-squire/secret-encryption/v1"


def fernet(secret_key: str) -> Fernet:
    hk = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=HKDF_INFO)
    key = hk.derive(secret_key.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key))


def decrypt(secret_key: str, stored: str) -> str | None:
    """None means "could not decrypt" (wrong key, or not our scheme) --
    distinct from "" (a genuinely empty stored secret), so the caller can
    tell the difference and warn instead of silently writing an empty
    value over whatever the destination had.
    """
    if not stored:
        return ""
    if not stored.startswith(ENC_PREFIX):
        return stored  # legacy/plaintext value, same tolerance as app/crypto.py
    try:
        return fernet(secret_key).decrypt(stored[len(ENC_PREFIX):].encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None


def encrypt(secret_key: str, plaintext: str) -> str:
    if not plaintext:
        return ""
    return ENC_PREFIX + fernet(secret_key).encrypt(plaintext.encode("utf-8")).decode("utf-8")
