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
"""Passphrase-encrypted archive sealing (Prompt C8, PLAN Section 7 "Backup
and restore" and the resolved KDF/cipher open item in Section 8).

A backup archive necessarily contains the instance's SECRET_KEY and OAuth
token store, so encryption is mandatory -- `ops/backup.py` never writes an
unencrypted archive to disk, and this module is the only place that knows
how to seal or open one. The two primitives are exactly what the plan
settles: the passphrase is stretched with Argon2id (the current
OWASP-recommended passphrase KDF), and the archive is sealed with
AES-256-GCM for authenticated encryption, so a corrupted or tampered
archive fails loudly on open rather than decrypting to garbage. Both come
from `cryptography`, already a dependency (see ops/crypto_mirror.py and
pyproject.toml's dependency comment) -- no new dependency is added.

Archive layout on disk: a small fixed-size header (magic, this module's
format version, the container format of the sealed plaintext -- tar.gz or
zip, the Argon2id parameters, a random salt, and a random nonce) followed
immediately by the AES-256-GCM ciphertext (which carries its own 16-byte
authentication tag, appended by `AESGCM.encrypt`). The header is never
secret -- it has to be readable before the passphrase can even be tried --
but it is also fed to AES-GCM as associated data, so tampering with any
header field (for instance, downgrading the Argon2id cost to make
brute-forcing cheaper) invalidates the authentication tag on open, not
just the ciphertext.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

FORMAT_VERSION = 1

# The container format of the *plaintext* payload once decrypted -- stored
# in the encrypted header (not the manifest inside the payload) because
# something has to say how to parse the payload before it can be parsed.
CONTAINER_TAR_GZ = 0
CONTAINER_ZIP = 1

_MAGIC = b"JSQB"  # "Job SQuire Backup"
_SALT_LEN = 16
_NONCE_LEN = 12  # standard AES-GCM nonce size
_KEY_LEN = 32  # AES-256
# magic, format_version, container_format, time_cost, memory_cost_kib, lanes, salt, nonce
_HEADER = struct.Struct(">4sBBIIB16s12s")

# OWASP's second recommended Argon2id option (prioritize resistance when
# memory is not constrained: https://cheatsheetseries.owasp.org/cheatsheets/
# Password_Storage_Cheat_Sheet.html). A backup is created and opened rarely,
# so spending a fraction of a second of Argon2id work is a good trade
# against an attacker who obtains a copy of the archive file.
DEFAULT_TIME_COST = 3
DEFAULT_MEMORY_COST_KIB = 65536  # 64 MiB
DEFAULT_LANES = 4

RandomBytes = Callable[[int], bytes]


class BackupCryptoError(RuntimeError):
    """The archive is malformed, truncated, or its format is unsupported."""


class WrongPassphraseError(BackupCryptoError):
    """AES-GCM authentication failed on open.

    This is the *only* failure mode once the header parses cleanly: a wrong
    passphrase and a corrupted/tampered ciphertext both fail the same way
    (GCM cannot tell them apart), so the message says so plainly rather
    than guessing which one happened.
    """


def _derive_key(passphrase: str, *, salt: bytes, time_cost: int, memory_cost_kib: int, lanes: int) -> bytes:
    kdf = Argon2id(salt=salt, length=_KEY_LEN, iterations=time_cost, lanes=lanes, memory_cost=memory_cost_kib)
    return kdf.derive(passphrase.encode("utf-8"))


def seal(
    plaintext: bytes,
    passphrase: str,
    *,
    container_format: int = CONTAINER_TAR_GZ,
    time_cost: int = DEFAULT_TIME_COST,
    memory_cost_kib: int = DEFAULT_MEMORY_COST_KIB,
    lanes: int = DEFAULT_LANES,
    random_bytes: RandomBytes = os.urandom,
) -> bytes:
    """Argon2id-stretch `passphrase` and seal `plaintext` with AES-256-GCM.

    Returns the full on-disk archive bytes: header followed by ciphertext.
    A fresh random salt and nonce are generated on every call, so sealing
    the same plaintext with the same passphrase twice never produces the
    same bytes.
    """
    if not passphrase:
        raise BackupCryptoError("A non-empty passphrase is required to seal a backup archive.")
    salt = random_bytes(_SALT_LEN)
    nonce = random_bytes(_NONCE_LEN)
    header = _HEADER.pack(_MAGIC, FORMAT_VERSION, container_format, time_cost, memory_cost_kib, lanes, salt, nonce)
    key = _derive_key(passphrase, salt=salt, time_cost=time_cost, memory_cost_kib=memory_cost_kib, lanes=lanes)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, header)
    return header + ciphertext


@dataclass(frozen=True)
class OpenedPayload:
    plaintext: bytes
    container_format: int


def open_sealed(sealed: bytes, passphrase: str) -> OpenedPayload:
    """Decrypt an archive produced by `seal`.

    Raises WrongPassphraseError if the passphrase is wrong or the archive
    was tampered with/corrupted after sealing, or BackupCryptoError for
    anything that isn't even a recognizable job-squire backup (too short,
    bad magic, or an encryption format version this CLI doesn't support).
    """
    if len(sealed) < _HEADER.size:
        raise BackupCryptoError("Not a job-squire backup archive (file is too small to contain a valid header).")
    header = sealed[: _HEADER.size]
    magic, version, container_format, time_cost, memory_cost_kib, lanes, salt, nonce = _HEADER.unpack(header)
    if magic != _MAGIC:
        raise BackupCryptoError("Not a job-squire backup archive (bad magic header).")
    if version != FORMAT_VERSION:
        raise BackupCryptoError(
            f"Unsupported backup archive encryption format version {version} -- "
            f"this CLI supports version {FORMAT_VERSION}. Restore it with a matching job-squire-cli version."
        )
    key = _derive_key(passphrase, salt=salt, time_cost=time_cost, memory_cost_kib=memory_cost_kib, lanes=lanes)
    ciphertext = sealed[_HEADER.size :]
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, header)
    except InvalidTag as exc:
        raise WrongPassphraseError(
            "Wrong passphrase, or the archive is corrupted or was tampered with -- could not decrypt."
        ) from exc
    return OpenedPayload(plaintext=plaintext, container_format=container_format)
