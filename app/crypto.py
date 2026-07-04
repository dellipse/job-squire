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
import json
import logging
import os

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


# ---------------------------------------------------------------------------
# Encrypted JSON file helpers
#
# Used for on-disk state that holds live secrets but is shared across processes
# via a file rather than the database — currently the OAuth access-token store
# (DATA_DIR/oauth_tokens.json), which holds live bearer tokens. The whole JSON
# document is encrypted with the same SECRET_KEY-derived Fernet key used for
# database secrets, so tokens are not readable at rest. Writes are atomic
# (temp file + os.replace) and the file is chmod 0600 so a torn read across the
# web/MCP processes can't happen and the file isn't world-readable.
# ---------------------------------------------------------------------------

def load_encrypted_json(path, secret_key, default=None):
    """Load JSON written by :func:`dump_encrypted_json`.

    Backward compatible: if ``path`` holds a legacy *plaintext* JSON document
    (written before encryption was added), it is still parsed and returned, and
    a warning is logged. It will be encrypted the next time it is written.
    Returns ``default`` (or ``{}``) when the file is missing, empty, or cannot
    be decrypted (e.g. SECRET_KEY changed).
    """
    fallback = {} if default is None else default
    try:
        with open(path, "r") as fh:
            raw = fh.read().strip()
    except OSError:
        return fallback
    if not raw:
        return fallback
    if raw.startswith(_PREFIX):
        body = raw[len(_PREFIX):]
        try:
            raw = _fernet(secret_key).decrypt(body.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            logger.warning("crypto.load_encrypted_json: InvalidToken for %s — "
                           "SECRET_KEY may have changed; treating as empty", path)
            return fallback
    else:
        logger.warning("crypto.load_encrypted_json: %s is unencrypted plaintext — "
                       "it will be re-encrypted on the next write", path)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def dump_encrypted_json(path, secret_key, obj):
    """Serialize ``obj`` to JSON and write it to ``path`` encrypted at rest.

    The write is atomic (temp file + ``os.replace``) and the resulting file is
    chmod 0600 so it is not readable by other users on the host.
    """
    token = _fernet(secret_key).encrypt(json.dumps(obj).encode("utf-8"))
    data = _PREFIX + token.decode("utf-8")
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        fh.write(data)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
