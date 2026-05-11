"""Extracteur pour les reçus Masrvi (PATRINET NETWORKING).

Échantillons test.txt :
- Ligne 53 : "Succés Date e . Com" Panement facture Code depuis compte Mame
  1500.00 MRU(...) payé chez 019370 PATRVNET NETWORKING qnenesoomss) OK"
- Ligne 65 : "Succés Paiement facture depuis comple 1490.00 MRU(...)
  paye chez 019370 PATRINET NETWORKING [REF165021159]. C"
- Ligne 55 : "Succés Date et heure'Z6/ll/20251605 Commergent PATRINET
  Code 019370 Momant. -1500,00 MRU"
"""

from __future__ import annotations

import re

from .base import BaseExtractor, ExtractionResult
from ..normalizer import normalize_amount, normalize_datetime, normalize_txn_id


# Les classes de "chiffres" tolèrent les confusions OCR fréquentes (O/o pris
# pour 0). On normalise ensuite UNIQUEMENT le groupe capturé via
# _digitize() — pas tout le texte (ce qui transformait "Montant" en "M0ntant"
# et fragilisait toutes les autres regex).
_D = r"[0-9Oo]"
_AMOUNT_RE = re.compile(rf"({_D}{{2,6}}[.,]{_D}{{2}})\s*MRU", re.IGNORECASE)
_AMOUNT_PLAIN_RE = re.compile(
    rf"-?\s*({_D}{{1,3}}(?:[\s.]?{_D}{{3}})?)[.,]{_D}{{2}}\s*MRU",
    re.IGNORECASE,
)
# Sens inverse "MRU 1000.00" pour le format générique vu chez Masrvi sans le
# préfixe "Paiement facture depuis compte" (OCR très bruité où ces mots sont
# perdus).
_AMOUNT_REVERSE_RE = re.compile(rf"MRU\s*({_D}{{2,6}}[.,]{_D}{{2}})", re.IGNORECASE)


def _digitize(s: str) -> str:
    """Normalise un candidat montant :
    - O/o → 0 (confusion OCR fréquente)
    - retire le séparateur de milliers (1.000 → 1000) en se basant sur la
      position : un "." suivi d'exactement 3 chiffres et précédé d'un
      chiffre est un séparateur, pas un point décimal.
    """
    s = s.replace("O", "0").replace("o", "0")
    s = re.sub(r"(?<=\d)\.(?=\d{3}(?!\d))", "", s)
    return s
_TXN_RE = re.compile(r"REF\s*[:\s]*([A-Z0-9]{6,20})", re.IGNORECASE)
_DATE_RE = re.compile(
    r"(?:Date\s*(?:et|and)\s*[hH]eure|Date\s*and\s*time)\s*[:'.\s]*"
    r"([0-9OoIl/\-]{6,12}[ ,]+[0-9OoIl: AMP]{4,15})",
    re.IGNORECASE,
)
_DATE_LOOSE_RE = re.compile(
    r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\s*[, ]+\s*(\d{1,2}[:hH]\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)"
)
_DATE_COMPACT_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})(\d{2})(\d{2})")
# Format Masrvi compact "Dateetheure:DD/MM/YYYYHH:MM" (tout collé, sans secondes).
# Année 4 chiffres non-greedy pour ne pas avaler les chiffres de l'heure.
_DATE_GLUED_RE = re.compile(
    r"Dat[ea]?\s*et?\s*h?eu?re?\s*[:.]?\s*"
    r"(\d{1,2}/\d{1,2}/\d{2,4}?)\s*"
    r"(\d{1,2}[:.]\d{2}(?:[:.]\d{2})?)",
    re.IGNORECASE,
)


class MasrviExtractor(BaseExtractor):
    name = "masrvi"

    def detect(self, text: str) -> float:
        score = 0.0
        markers = (
            (r"Paiement\s+facture\s+depuis", 0.5),
            (r"PATRINET\s+NETWORKING", 0.3),
            # \s* après REF pour gérer "REF 218719367" (avec espace).
            (r"\bREF\s*\d{6,}", 0.4),
            (r"\bSucc[eé]s\b", 0.15),
            (r"Commer[cgç]ant", 0.1),
            # Markers du format Masrvi compact "Dateetheure:.. Code:A2 Montant:-X,XX MRU"
            (r"\bA2\s*CONNECT\b", 0.3),
            (r"Code\s*[:.]?\s*A2\b", 0.25),
            (r"Montant\s*[:.]?\s*-\s*\d", 0.25),
        )
        for pattern, weight in markers:
            if re.search(pattern, text, re.IGNORECASE):
                score += weight
        return min(score, 1.0)

    def extract(self, text: str, ocr_result=None) -> ExtractionResult:
        result = ExtractionResult(template=self.name, detect_score=self.detect(text))
        ext = result.extracted

        # Les regex montant acceptent [0-9Oo] dans les positions chiffres :
        # "1 OOO,OO MRU" matche directement. On ne touche PAS au reste du texte.
        m = (
            _AMOUNT_PLAIN_RE.search(text)
            or _AMOUNT_RE.search(text)
            or _AMOUNT_REVERSE_RE.search(text)
        )
        if m:
            raw = _digitize(m.group(1))
            ext.montant = normalize_amount(raw)
            result.field_confidence["montant"] = self._confidence(ocr_result, m.group(1))

        m = _TXN_RE.search(text)
        if m:
            ext.txn_id = normalize_txn_id("REF" + m.group(1))
            result.field_confidence["txn_id"] = self._confidence(ocr_result, m.group(1))

        # Priorité 1 : format compact collé "Dateetheure:DD/MM/YYYYHH:MM"
        m = _DATE_GLUED_RE.search(text)
        if m:
            raw = f"{m.group(1)} {m.group(2)}"
            iso = normalize_datetime(raw)
            if iso:
                ext.date_heure = iso
                result.field_confidence["date_heure"] = self._confidence(ocr_result, raw)
        if ext.date_heure is None:
            m = _DATE_RE.search(text)
            if m:
                ext.date_heure = normalize_datetime(m.group(1))
                result.field_confidence["date_heure"] = self._confidence(ocr_result, m.group(1))
            else:
                m = _DATE_LOOSE_RE.search(text)
                if m:
                    raw = f"{m.group(1)} {m.group(2)}"
                    ext.date_heure = normalize_datetime(raw)
                    result.field_confidence["date_heure"] = self._confidence(ocr_result, raw)
                else:
                    m = _DATE_COMPACT_RE.search(text)
                    if m:
                        raw = f"{m.group(1)} {m.group(2)}:{m.group(3)}"
                        ext.date_heure = normalize_datetime(raw)
                        result.field_confidence["date_heure"] = self._confidence(ocr_result, raw)

        return result
