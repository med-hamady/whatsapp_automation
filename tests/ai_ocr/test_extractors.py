"""Tests des extracteurs sur des fragments OCR réels (issus de test.txt).

Ne nécessite ni PaddleOCR ni connexion réseau : on alimente directement le
texte OCR aux extracteurs et on vérifie le JSON produit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from whatsapp_automation.ai_ocr.extractors import extract


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "samples.json"


def _load_samples():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("sample", _load_samples(), ids=lambda s: s["id"])
def test_extraction(sample):
    result = extract(sample["text"])

    assert result.template == sample["expected_template"], (
        f"template attendu {sample['expected_template']}, obtenu {result.template}"
    )
    assert result.extracted.montant == sample["expected_montant"], (
        f"montant attendu {sample['expected_montant']}, obtenu {result.extracted.montant}"
    )

    if "expected_txn_id_prefix" in sample:
        prefix = sample["expected_txn_id_prefix"]
        assert result.extracted.txn_id is not None, "txn_id manquant"
        assert result.extracted.txn_id.startswith(prefix), (
            f"txn_id {result.extracted.txn_id} ne commence pas par {prefix}"
        )

    if "expected_date_starts" in sample:
        assert result.extracted.date_heure is not None, "date_heure manquante"
        assert result.extracted.date_heure.startswith(
            sample["expected_date_starts"]
        ), (
            f"date_heure {result.extracted.date_heure} != {sample['expected_date_starts']}*"
        )


def test_overall_confidence_when_all_fields_present():
    sample = _load_samples()[0]
    result = extract(sample["text"])
    assert result.extracted.montant is not None
    assert 0.0 <= result.overall_confidence() <= 1.0


def test_generic_fallback_for_unknown_template():
    text = "Reçu inconnu MRU 750 transaction 12345678901234"
    result = extract(text)
    assert result.template == "generic"
    assert result.extracted.montant == 750
