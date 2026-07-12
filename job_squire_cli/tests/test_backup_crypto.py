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
"""Argon2id + AES-256-GCM archive sealing (Prompt C8).

Every test uses cheap Argon2id parameters (time_cost=1, memory_cost=8 MiB,
lanes=1) purely for test speed -- ops/backup.py's real callers use
backup_crypto.DEFAULT_* (see test_backup.py, which exercises those).
"""
import pytest

from job_squire_cli.ops import backup_crypto as bc

_CHEAP = dict(time_cost=1, memory_cost_kib=8192, lanes=1)


def test_seal_open_roundtrip():
    payload = b"hello job squire" * 100
    sealed = bc.seal(payload, "correct horse battery staple", **_CHEAP)
    opened = bc.open_sealed(sealed, "correct horse battery staple")
    assert opened.plaintext == payload
    assert opened.container_format == bc.CONTAINER_TAR_GZ


def test_seal_records_requested_container_format():
    sealed = bc.seal(b"data", "pw", container_format=bc.CONTAINER_ZIP, **_CHEAP)
    opened = bc.open_sealed(sealed, "pw")
    assert opened.plaintext == b"data"
    assert opened.container_format == bc.CONTAINER_ZIP


def test_wrong_passphrase_raises_wrong_passphrase_error():
    sealed = bc.seal(b"secret payload", "right-passphrase", **_CHEAP)
    with pytest.raises(bc.WrongPassphraseError):
        bc.open_sealed(sealed, "wrong-passphrase")


def test_tampered_ciphertext_raises_wrong_passphrase_error():
    """AES-GCM authentication catches a corrupted/tampered archive just
    like a wrong passphrase -- the two are indistinguishable to GCM, so
    both surface as WrongPassphraseError with a message that says so."""
    sealed = bc.seal(b"secret payload", "pw", **_CHEAP)
    tampered = bytearray(sealed)
    tampered[-1] ^= 0xFF
    with pytest.raises(bc.WrongPassphraseError):
        bc.open_sealed(bytes(tampered), "pw")


def test_tampered_header_field_also_invalidates_authentication():
    """The header is fed to AES-GCM as associated data, so mutating a
    header field that doesn't even feed the KDF (container_format, byte
    offset 5 -- see _HEADER's struct layout) still invalidates the
    authentication tag on open."""
    sealed = bc.seal(b"secret payload", "pw", container_format=bc.CONTAINER_TAR_GZ, **_CHEAP)
    tampered = bytearray(sealed)
    tampered[5] = bc.CONTAINER_ZIP
    with pytest.raises(bc.WrongPassphraseError):
        bc.open_sealed(bytes(tampered), "pw")


def test_truncated_file_raises_backup_crypto_error_not_wrong_passphrase():
    with pytest.raises(bc.BackupCryptoError):
        bc.open_sealed(b"too short to be a real archive", "pw")


def test_bad_magic_raises_backup_crypto_error():
    sealed = bytearray(bc.seal(b"secret payload", "pw", **_CHEAP))
    sealed[0:4] = b"XXXX"
    with pytest.raises(bc.BackupCryptoError, match="magic"):
        bc.open_sealed(bytes(sealed), "pw")


def test_unsupported_format_version_raises_backup_crypto_error():
    sealed = bytearray(bc.seal(b"secret payload", "pw", **_CHEAP))
    sealed[4] = 99  # format_version byte
    with pytest.raises(bc.BackupCryptoError, match="format version"):
        bc.open_sealed(bytes(sealed), "pw")


def test_empty_passphrase_rejected_at_seal_time():
    with pytest.raises(bc.BackupCryptoError):
        bc.seal(b"secret payload", "", **_CHEAP)


def test_salt_and_nonce_are_random_per_call():
    sealed1 = bc.seal(b"same plaintext", "same passphrase", **_CHEAP)
    sealed2 = bc.seal(b"same plaintext", "same passphrase", **_CHEAP)
    assert sealed1 != sealed2  # fresh salt/nonce -> different bytes despite identical inputs
    # both still open correctly with the same passphrase
    assert bc.open_sealed(sealed1, "same passphrase").plaintext == b"same plaintext"
    assert bc.open_sealed(sealed2, "same passphrase").plaintext == b"same plaintext"


def test_header_carries_salt_nonce_and_argon2_params():
    sealed = bc.seal(b"x", "pw", time_cost=2, memory_cost_kib=16384, lanes=1)
    header = sealed[: bc._HEADER.size]
    magic, version, container_format, time_cost, memory_cost_kib, lanes, salt, nonce = bc._HEADER.unpack(header)
    assert magic == b"JSQB"
    assert version == bc.FORMAT_VERSION
    assert time_cost == 2
    assert memory_cost_kib == 16384
    assert lanes == 1
    assert len(salt) == 16
    assert len(nonce) == 12
