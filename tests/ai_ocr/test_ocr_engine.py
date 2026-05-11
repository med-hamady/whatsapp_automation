"""Tests unitaires pour ocr_engine sans charger de modèle ONNX (pas
d'instanciation _Engine, on teste seulement les helpers)."""

from __future__ import annotations

from whatsapp_automation.ai_ocr.engine import OcrBox, OcrResult, confidence_for_span


def _make(boxes):
    return OcrResult(
        boxes=[OcrBox(text=t, confidence=c) for t, c in boxes],
        text=" ".join(t for t, _ in boxes),
    )


def test_confidence_for_span_empty():
    assert confidence_for_span(_make([]), "990") == 0.0
    assert confidence_for_span(_make([]), "") == 0.0
    assert confidence_for_span(_make([("abc", 1.0)]), "") == 0.0


def test_confidence_for_span_exact_match():
    r = _make([("MRU 990", 0.95), ("Effectue", 0.5)])
    # "990" est inclus dans "MRU990" (compact), len 3 vs 6 = 50% < 70% → rejeté
    # mais le compact "MRU990" est inclus dans le digits target seulement si
    # value="MRU990" — ici value="990" donc on cherche "990" ⊂ "MRU990" et
    # 3/6 = 50% < 70% → 0
    assert confidence_for_span(r, "990") == 0.0  # rejet anti faux-positif

    # Avec une box exactement "990", match strict
    r2 = _make([("990", 0.95)])
    assert confidence_for_span(r2, "990") == 0.95


def test_confidence_for_span_rejects_substring_match():
    """Le bug fixé : "25" ⊂ "2025" ne doit PAS retourner la confiance de 2025."""
    r = _make([("2025", 0.99), ("autre", 0.3)])
    # value="25" cherche dans compacts. "25" ⊂ "2025", mais 2/4 = 50% < 70% → 0.
    assert confidence_for_span(r, "25") == 0.0


def test_confidence_for_span_partial_match_above_threshold():
    """Si la box contient quasi-tout le candidat, on accepte."""
    r = _make([("MRU1500", 0.92)])
    # value="1500" : digits="1500" (4 chars) ⊂ compact="MRU1500" (7 chars).
    # 4/7 ≈ 57% < 70% → rejeté. Donc 0.
    assert confidence_for_span(r, "1500") == 0.0

    # Match plus serré : compact "1500" et value "1500" → 100%, accepté.
    r2 = _make([("1500", 0.92)])
    assert confidence_for_span(r2, "1500") == 0.92


def test_confidence_for_span_takes_max():
    r = _make([("990", 0.7), ("990", 0.9), ("autre", 0.99)])
    assert confidence_for_span(r, "990") == 0.9
