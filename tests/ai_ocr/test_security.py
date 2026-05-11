"""Tests de sécurité : path traversal et bornes sample_id."""

from __future__ import annotations

import pytest

from whatsapp_automation.ai_ocr.dataset.writer import (
    _safe_sample_dir,
    load_sample,
    sample_dir,
    write_label,
)


@pytest.mark.parametrize(
    "bad_id",
    [
        "../etc/passwd",
        "..\\..\\..\\Windows\\System32\\config",
        "2026-05-07/../../etc",
        "2026-05-07/abc",                    # uuid trop court
        "2026-05-07/" + "g" * 32,            # caractères non-hex
        "2026-05-07/" + "a" * 33,            # uuid trop long
        "26-5-7/" + "a" * 32,                # date pas au format
        "../../../../../../../../tmp/x",
        "",
        "/",
        "//etc/passwd",
        "C:\\Windows\\System32",
    ],
)
def test_safe_sample_dir_rejects(bad_id):
    """_safe_sample_dir doit retourner None pour tout sample_id non conforme."""
    assert _safe_sample_dir(bad_id) is None


def test_sample_dir_raises_on_invalid():
    with pytest.raises(ValueError):
        sample_dir("../../../etc")


def test_load_sample_returns_none_on_invalid():
    assert load_sample("../../../etc") is None


def test_write_label_rejects_invalid():
    assert write_label("../../../etc", {"montant": 100}) is False


@pytest.mark.parametrize(
    "good_id",
    [
        "2026-05-07/" + "a" * 32,
        "2026-05-07/" + "0123456789abcdef" * 2,
        "1999-12-31/deadbeef" + "0" * 24,
    ],
)
def test_safe_sample_dir_accepts_valid_format(good_id):
    """Les IDs au format YYYY-MM-DD/<hex32> sont bien acceptés (même si le
    dossier n'existe pas physiquement)."""
    sd = _safe_sample_dir(good_id)
    assert sd is not None
    # Le path retourné doit être strictement sous le store_root.
    from whatsapp_automation.ai_ocr.dataset.writer import store_root

    assert sd.resolve().is_relative_to(store_root().resolve())
