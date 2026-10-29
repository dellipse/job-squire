# Copyright (C) 2026 D. Brandmeyer
# Licensed under the GNU Affero General Public License v3 or later.
"""Tests for the build-time distrusted-root guard (scripts/check_distrusted_roots.py).

Covers:
  * the certifi version pinned in requirements.txt no longer trusts the
    eMudhra roots removed after the October 2026 key compromise.
  * the guard fails on a bundle that still holds one, and --strip-system
    removes it from the system bundle without touching any other certificate.
"""
import importlib.util
import pathlib

import certifi

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "check_distrusted_roots.py"
_spec = importlib.util.spec_from_file_location("check_distrusted_roots", _SCRIPT)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _real_bundle() -> str:
    with open(certifi.where(), encoding="ascii") as f:
        return f.read()


def _pem_block(pem_text: str, index: int) -> str:
    return list(guard._PEM_RE.finditer(pem_text))[index].group(0)


def _poison(pem_text: str, monkeypatch) -> str:
    """Mark the bundle's first certificate as distrusted, for a known-bad input."""
    first = _pem_block(pem_text, 0)
    fp = guard._fingerprint(guard._PEM_RE.match(first).group(1))
    monkeypatch.setitem(guard.DISTRUSTED, fp, "test root")
    return first


class TestPinnedCertifi:
    def test_pinned_certifi_has_no_distrusted_roots(self):
        assert guard.find_distrusted(_real_bundle()) == []

    def test_emsign_g1_still_trusted(self):
        # Mozilla kept emSign Root CA - G1; the guard must not drop it.
        assert "emSign Root CA - G1" in _real_bundle()


class TestGuard:
    def test_fails_when_certifi_bundle_holds_a_distrusted_root(self, tmp_path, monkeypatch):
        bundle = _real_bundle()
        _poison(bundle, monkeypatch)
        (tmp_path / "certifi.pem").write_text(bundle, encoding="ascii")
        (tmp_path / "system.pem").write_text(bundle, encoding="ascii")
        rc = guard.main(["--strip-system",
                         "--certifi-bundle", str(tmp_path / "certifi.pem"),
                         "--system-bundle", str(tmp_path / "system.pem")])
        assert rc == 1

    def test_fails_on_system_bundle_without_strip(self, tmp_path, monkeypatch):
        bundle = _real_bundle()
        first = _pem_block(bundle, 0)
        (tmp_path / "system.pem").write_text(bundle, encoding="ascii")
        (tmp_path / "certifi.pem").write_text(bundle.replace(first, ""), encoding="ascii")
        _poison(bundle, monkeypatch)
        rc = guard.main(["--certifi-bundle", str(tmp_path / "certifi.pem"),
                         "--system-bundle", str(tmp_path / "system.pem")])
        assert rc == 1

    def test_strip_system_removes_only_the_distrusted_root(self, tmp_path, monkeypatch):
        bundle = _real_bundle()
        first = _pem_block(bundle, 0)
        second = _pem_block(bundle, 1)
        _poison(bundle, monkeypatch)
        (tmp_path / "system.pem").write_text(bundle, encoding="ascii")
        (tmp_path / "certifi.pem").write_text(bundle.replace(first, ""), encoding="ascii")
        rc = guard.main(["--strip-system",
                         "--certifi-bundle", str(tmp_path / "certifi.pem"),
                         "--system-bundle", str(tmp_path / "system.pem")])
        stripped = (tmp_path / "system.pem").read_text(encoding="ascii")
        assert rc == 0
        assert first not in stripped
        assert second in stripped
        assert stripped.count("BEGIN CERTIFICATE") == bundle.count("BEGIN CERTIFICATE") - 1
