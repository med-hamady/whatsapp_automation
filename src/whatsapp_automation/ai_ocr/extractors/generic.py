"""Extracteur générique : utilisé en dernier recours quand aucun template
spécifique ne matche assez fort. Reproduit la logique historique de
webhook.php (split sur "MRU") et tente quelques heuristiques sur date/ID.
"""

from __future__ import annotations

import re

from .base import BaseExtractor, ExtractionResult
from ..normalizer import normalize_amount, normalize_datetime, normalize_txn_id


# (?<!\d) empêche de capturer "16756" depuis le code marchand "016756" :
# le caractère qui précède notre nombre ne doit pas être un chiffre.
_AMOUNT_NEAR_MRU_RE = re.compile(
    r"(?:MRU\s*(?<!\d)([1-9][\d\s,.]{1,11})|(?<!\d)([1-9][\d\s,.]{1,11})\s*MRU)",
    re.IGNORECASE,
)
_LONG_DIGIT_RE = re.compile(r"\b(\d{12,20})\b")
_DATE_HINT_RE = re.compile(
    r"(\d{1,2}[\-/—]\d{1,2}[\-/—]\d{2,4}(?:\s+\d{1,2}[:.]\d{2}(?:[:.]\d{2})?)?)"
)


class GenericExtractor(BaseExtractor):
    name = "generic"

    def detect(self, text: str) -> float:
        return 0.05 if "MRU" in text.upper() else 0.0

    def extract(self, text: str, ocr_result=None) -> ExtractionResult:
        result = ExtractionResult(template=self.name, detect_score=self.detect(text))
        ext = result.extracted

        # On collecte tous les candidats avec leur confiance OCR puis on
        # garde le meilleur. Avant on s'arrêtait à la 1ère valeur valide,
        # ce qui sélectionnait un parasite si présent avant le vrai montant.
        best_amount = None
        best_amount_conf = -1.0
        best_amount_raw = None
        for match in _AMOUNT_NEAR_MRU_RE.finditer(text):
            raw = match.group(1) or match.group(2)
            value = normalize_amount(raw)
            if not value:
                continue
            conf = self._confidence(ocr_result, raw)
            if conf > best_amount_conf:
                best_amount = value
                best_amount_conf = conf
                best_amount_raw = raw
        if best_amount is not None:
            ext.montant = best_amount
            result.field_confidence["montant"] = (
                best_amount_conf if best_amount_conf > 0 else self._confidence(ocr_result, best_amount_raw)
            )

        m = _LONG_DIGIT_RE.search(text)
        if m:
            ext.txn_id = normalize_txn_id(m.group(1))
            result.field_confidence["txn_id"] = self._confidence(ocr_result, m.group(1))

        m = _DATE_HINT_RE.search(text)
        if m:
            iso = normalize_datetime(m.group(1))
            if iso is not None:
                ext.date_heure = iso
                result.field_confidence["date_heure"] = self._confidence(ocr_result, m.group(1))

        return result
