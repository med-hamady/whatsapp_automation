"""Extracteur pour les reçus Sedad Bank.

Échantillon test.txt ligne 71 :
"PAT... 990.0 ... PAIEMENT ... SEDAD BANK ... 99000 ... PATRIE NET 01471
 17:o9:312:»11—2o25 ... TR06124615221"

L'OCR Sedad est très bruité (texte arabe entremêlé). Les ancres fiables sont
le préfixe "TR" suivi de chiffres pour l'ID, et "SEDAD BANK".
"""

from __future__ import annotations

import re

from .base import BaseExtractor, ExtractionResult
from ..normalizer import normalize_amount, normalize_datetime, normalize_txn_id


_TXN_RE = re.compile(r"\b(TR0?\d{8,15})\b")
# Format européen "1.500,00" avec point pour les milliers, OU "990,00".
# Le lookbehind (?<!\d) empêche de capturer "1471" depuis "01471" (code commerçant).
_AMOUNT_EURO_RE = re.compile(r"(?<!\d)([1-9]\d{0,2}\.\d{3})[.,]\d{2}")
_AMOUNT_DECIMAL_RE = re.compile(r"(?<!\d)([1-9]\d{1,5})[.,]\d{1,2}\b")
# Sedad PLAIN volontairement supprimé : sur OCR très bruité (sans virgule
# décimale ni anchor MRU), tout fallback générique attrape des artefacts
# (ex : "4802" issu du collage heure+date "10:35:4802-05-2026"). Mieux vaut
# retourner None et laisser l'opérateur corriger que de retourner faux.
# Sens direct : "DD-MM-YYYY HH:MM:SS" (souvent collé "27-04-202612:14:37").
_DATE_DIRECT_RE = re.compile(
    r"([0-9OoIl]{1,2}[\-—/][0-9OoIl]{1,2}[\-—/][0-9OoIl]{2}(?:\d{2})?)"
    r"\s*"
    r"([0-9OoIl]{1,2}[:.][0-9OoIl]{2}(?:[:.][0-9OoIl]{2})?)"
)
# Sens reverse (RTL) : "HH:MM:SS DD-MM-YYYY" (parfois collé).
_DATE_RE = re.compile(
    r"(\d{1,2}[:.][0-9OoIl]{2}(?:[:.][0-9OoIl]{2})?)\s*\D{0,3}"
    r"([0-9OoIl]{1,2}[\-—/][0-9OoIl]{1,2}[\-—/][0-9OoIl]{2,4})"
)


class SedadExtractor(BaseExtractor):
    name = "sedad"

    def detect(self, text: str) -> float:
        score = 0.0
        markers = (
            (r"SEDAD\s*BANK", 0.5),
            (r"\bPAIEMENT\b", 0.2),
            (r"\bTR0?\d{8,}\b", 0.4),
            (r"BMI", 0.1),
        )
        for pattern, weight in markers:
            if re.search(pattern, text, re.IGNORECASE):
                score += weight
        return min(score, 1.0)

    def extract(self, text: str, ocr_result=None) -> ExtractionResult:
        result = ExtractionResult(template=self.name, detect_score=self.detect(text))
        ext = result.extracted

        m = _TXN_RE.search(text)
        if m:
            ext.txn_id = normalize_txn_id(m.group(1))
            result.field_confidence["txn_id"] = self._confidence(ocr_result, m.group(1))

        # Priorité 1 : format européen "1.500,00" (avec séparateur de milliers)
        m = _AMOUNT_EURO_RE.search(text)
        if m:
            # Reconstruire "1500" en virant le point séparateur de milliers
            raw = m.group(1).replace(".", "")
            ext.montant = normalize_amount(raw)
            result.field_confidence["montant"] = self._confidence(ocr_result, m.group(1))
        else:
            m = _AMOUNT_DECIMAL_RE.search(text)
            if m:
                ext.montant = normalize_amount(m.group(1))
                result.field_confidence["montant"] = self._confidence(ocr_result, m.group(1))
            # Pas de fallback PLAIN : OCR Sedad bruité → préférer null à faux.

        # Priorité 1 : format direct "DD-MM-YYYY HH:MM:SS"
        m = _DATE_DIRECT_RE.search(text)
        if m:
            raw = f"{m.group(1)} {m.group(2)}"
            iso = normalize_datetime(raw)
            if iso:
                ext.date_heure = iso
                result.field_confidence["date_heure"] = self._confidence(ocr_result, raw)
        # Fallback : sens reverse (RTL)
        if ext.date_heure is None:
            m = _DATE_RE.search(text)
            if m:
                raw = f"{m.group(2)} {m.group(1)}"
                ext.date_heure = normalize_datetime(raw)
                result.field_confidence["date_heure"] = self._confidence(ocr_result, raw)

        return result
