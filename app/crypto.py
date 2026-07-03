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
"""Symmetric encryption for stored secrets (API keys, SMTP password).

The Fernet key is derived from the app SECRET_KEY via HKDF-SHA256, with a
domain-separation label so the derived key differs from the cookie-signing
use of the same SECRET_KEY. There is no extra secret to manage.

WARNING: SECRET_KEY is dual-purpose — it signs session cookies AND derives the
encryption key for all stored provider/SMTP/Anthropic credentials. Rotating
SECRET_KEY invalidates all stored secrets; users must re-enter them on the
Settings page after a rotation.
"""
import base64
import logging

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

# Ciphertext prefix identifies the encryption scheme so it can evolve later
# without breaking existing stored secrets.
_PREFIX = "enc:"
_HKDF_INFO = b"job-squire/secret-encryption/v1"


def _fernet(secret_key):
    hk = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO)
    key = hk.derive(secret_key.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(secret_key, plaintext):
    if plaintext is None or plaintext == "":
        return ""
    token = _fernet(secret_key).encrypt(plaintext.encode("utf-8"))
    return _PREFIX + token.decode("utf-8")


def decrypt(secret_key, stored):
    if not stored:
        return ""
    if not stored.startswith(_PREFIX):
        # Tolerate legacy/plaintext values, but flag them — a secret stored
        # without encryption is a credential-at-rest exposure that should be
        # re-saved so it gets encrypted. See issue #6.
        logger.warning("crypto.decrypt: value is not encrypted (no %r prefix) — "
                       "returning as-is; a secret may be stored in plaintext",
                       _PREFIX)
        return stored
    body = stored[len(_PREFIX):]
    try:
        return _fernet(secret_key).decrypt(body.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.warning("crypto.decrypt: InvalidToken — SECRET_KEY may have changed; stored value cannot be decrypted")
        return ""
