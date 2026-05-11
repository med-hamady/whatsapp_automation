"""Tests des normalisateurs."""

from __future__ import annotations

import pytest

from whatsapp_automation.ai_ocr.normalizer import (
    normalize_amount,
    normalize_datetime,
    normalize_txn_id,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1500", 1500),
        ("MRU 1500", 1500),
        ("1,500", 1500),
        ("1 500", 1500),
        ("1500.00", 1500),
        ("1,500.00", 1500),
        ("-990,00", 990),
        ("990.0", 990),
        ("0", None),
        ("", None),
        (None, None),
        # Plafond porté à 10M MRU pour autoriser les paiements business :
        ("9999999", 9999999),    # 10M-1 : accepté
        ("10000000", 10000000),  # 10M : accepté
        ("10000001", None),      # > plafond : rejeté avec warning
    ],
)
def test_normalize_amount(raw, expected):
    assert normalize_amount(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("16250529141 2008201 6", "16250529141200820"),
        ("Trs1D:0425112513420946064", "Trs1D04251125134209460"),
        ("ABC", None),
        (None, None),
        ("TR06124615221", "TR06124615221"),
    ],
)
def test_normalize_txn_id_keeps_alnum(raw, expected):
    out = normalize_txn_id(raw)
    if expected is None:
        assert out is None
    else:
        assert out is not None and out.startswith(expected[:10])


@pytest.mark.parametrize(
    "raw,starts_with",
    [
        ("29-05-25 14:12:02", "2025-05-29T14:12:02"),
        ("25-11-25 13:42:11", "2025-11-25T13:42:11"),
        ("26-11-25 16:54:59", "2025-11-26T16:54:59"),
        ("29—O5—25 14:12:02", "2025-05-29T14:12:02"),
        ("25/11/2025 17:20", "2025-11-25T17:20"),
    ],
)
def test_normalize_datetime(raw, starts_with):
    out = normalize_datetime(raw)
    assert out is not None and out.startswith(starts_with), f"got {out}"


def test_normalize_datetime_invalid():
    assert normalize_datetime("pas une date") is None
    assert normalize_datetime("") is None
    assert normalize_datetime(None) is None
