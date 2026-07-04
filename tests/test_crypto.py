# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for app/crypto.py — Fernet encryption of stored secrets.

Every provider API key, SMTP password, and Anthropic key is stored through
encrypt()/decrypt(). The properties that protect those credentials:

  * a value survives a full encrypt -> decrypt round trip unchanged;
  * empty/None inputs are handled without producing ciphertext;
  * a rotated SECRET_KEY (wrong key) fails closed, returning "" rather than
    raising or leaking a wrong plaintext;
  * legacy plaintext values (stored before encryption existed) pass through so
    the app keeps working, but are recognisably not encrypted.

These are pure functions, so no Flask app or database is needed.
"""
from app.crypto import _PREFIX, decrypt, encrypt

KEY = "unit-test-secret-key-aaaaaaaaaaaaaaaaaaaaaaaa"
OTHER_KEY = "a-different-secret-key-bbbbbbbbbbbbbbbbbbbbbbbb"


def test_round_trip():
    """A secret encrypted and decrypted with the same key is unchanged."""
    secret = "sk-ant-super-secret-value-123"
    token = encrypt(KEY, secret)
    assert token != secret, "ciphertext must not equal plaintext"
    assert token.startswith(_PREFIX), "ciphertext must carry the scheme prefix"
    assert decrypt(KEY, token) == secret


def test_round_trip_unicode():
    """Non-ASCII plaintext survives the round trip (UTF-8 handling)."""
    secret = "pässwörd-ümläut-\U0001f510"
    assert decrypt(KEY, encrypt(KEY, secret)) == secret


def test_encrypt_is_non_deterministic():
    """Two encryptions of the same plaintext differ but both decrypt correctly.

    Fernet embeds a random IV and timestamp, so identical inputs must not
    produce identical ciphertext (which would leak equality of secrets).
    """
    a = encrypt(KEY, "same-value")
    b = encrypt(KEY, "same-value")
    assert a != b
    assert decrypt(KEY, a) == decrypt(KEY, b) == "same-value"


def test_empty_and_none_encrypt_to_empty_string():
    """Empty/None plaintext yields '' — never a stray ciphertext blob."""
    assert encrypt(KEY, "") == ""
    assert encrypt(KEY, None) == ""


def test_empty_and_none_decrypt_to_empty_string():
    """Empty/None stored values decrypt to '' without error."""
    assert decrypt(KEY, "") == ""
    assert decrypt(KEY, None) == ""


def test_wrong_key_fails_closed():
    """Decrypting with a rotated/incorrect key returns '' (InvalidToken path).

    This mirrors a real SECRET_KEY rotation: stored secrets can no longer be
    read, and the app must degrade gracefully (prompt re-entry) rather than
    crash or surface a bogus plaintext.
    """
    token = encrypt(KEY, "value-encrypted-under-KEY")
    assert decrypt(OTHER_KEY, token) == ""


def test_legacy_plaintext_passthrough():
    """A stored value without the enc: prefix is returned as-is (legacy data)."""
    legacy = "stored-before-encryption-existed"
    assert not legacy.startswith(_PREFIX)
    assert decrypt(KEY, legacy) == legacy


def test_tampered_ciphertext_fails_closed():
    """A corrupted token (prefix present, body mangled) returns '' not garbage."""
    token = encrypt(KEY, "value")
    tampered = _PREFIX + token[len(_PREFIX):][:-4] + "XXXX"
    assert decrypt(KEY, tampered) == ""
