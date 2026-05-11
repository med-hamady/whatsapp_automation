"""Extracteur pour les reçus Bankily.

Templates types vus dans test.txt :
- "Paiement réussi! Nom du commercant: PATRINET NKTT ... Montant payé: MRU 1990
  Trs ID I 0425112513420946064 Date et heure 2 25-11-25 13:42:11 Effectué"
- "Payment Successful ! Merchant Name : PATRINET NKTT ... Amount Paid : MRU 1500
  Txn ID: 16250529141 2008201 6 Dale 8: Time: 29—O5—25 14:12:02 Done"
"""

from __future__ import annotations

import re

from .base import BaseExtractor, ExtractionResult
from ..normalizer import (
    normalize_amount,
    normalize_datetime,
    normalize_txn_id,
    parse_yymmdd_explicit,
)


# Les montants Bankily ne commencent jamais par 0. Le lookbehind (?<!\d)
# empêche le moteur regex de démarrer un match juste après le 0 initial du
# code marchand 016456 (ce qui capturait "16456 MRU").
_AMOUNT_RE = re.compile(
    r"(?:Montant(?:\s*pay[ée]?|\s*envoy[ée]?)?|Amount\s*(?:Paid|Sent)?|"
    r"Montant\s+envoy[ée]?)"
    r"\s*[:.IiTt]*\s*"
    r"(?:MRU\s*(?<!\d)([1-9]\d{1,5})|(?<!\d)([1-9]\d{1,5})(?:[.,]\d+)?\s*MRU)",
    re.IGNORECASE,
)
# Fallback : "MRU <num>" prioritaire. Sens inverse "<num>(.X)? MRU" accepté
# si num ne commence pas par 0 (cas "016456 MRU"). La partie (?:[.,]\d+)? gère
# "1000.0MRU" collé du format ligne d'historique Bankily.
_AMOUNT_FALLBACK_RE = re.compile(
    r"MRU\s*(?<!\d)([1-9]\d{1,5})|(?<!\d)([1-9]\d{1,5})(?:[.,]\d+)?\s*MRU",
    re.IGNORECASE,
)
# P2P arabe : on cherche le téléphone du receveur (8 chiffres) puis le
# montant qui suit, avec du bruit possible entre. Ex : "34610101 lal 990:".
# On a aussi le cas "990:ll134610101" (montant avant phone répété, avec un
# "1" parasite) → géré par le 2e pattern qui accepte jusqu'à 10 digits du
# phone (8 réels + 2 parasites possibles).
_AMOUNT_P2P_RE = re.compile(
    r"\b\d{8}\b\s*\D{0,8}\s*(?<!\d)([1-9]\d{2,4})\b"
    r"|"
    r"(?<!\d)([1-9]\d{2,4})\s*[:.]\s*\D{0,4}\d{8,10}\b",
    re.IGNORECASE,
)

_TXN_RE = re.compile(
    r"(?:Trs\s*ID|Txn\s*ID|Txn1?D|Trs1?D)\s*[:.IiTt\s]*([\d\sOoIl]{10,30})",
    re.IGNORECASE,
)
# Fallback : Bankily encode des Txn ID de 17-20 chiffres consécutifs (parfois
# fragmentés par un caractère bruit). On accepte 15-22 chiffres.
_TXN_FALLBACK_RE = re.compile(r"\b(\d{15,22})\b")

_DATE_ANCHOR_RE = re.compile(
    r"D[ae]l?[te]\s*(?:et\s*[hH]eure|[&8]\s*Time|and\s*time)",
    re.IGNORECASE,
)

# Année 2 OU 4 chiffres : `\d{2}(?:\d{2})?` permet au moteur regex de
# backtracker pour ne pas avaler les chiffres de l'heure suivante quand
# date et heure sont collées sans espace (ex : "05-05-2618:11:21").
_DATETIME_RE = re.compile(
    r"([0-9OoIlQ]{1,2}[\-—–/ ][0-9OoIlQ]{1,2}[\-—–/ ][0-9OoIlQ]{2}(?:\d{2})?)"
    r"\s*[,T ]?\s*"
    r"([0-9OoIlQ]{1,2}[:.h][0-9OoIlQ]{2}(?:[:.h][0-9OoIlQ]{2})?)"
)
# Layout Bankily arabe (RTL) : "HH:MM:SSDD-MM-YY" (heure puis date, souvent
# concaténées sans espace après OCR). On accepte 0 ou plus d'espaces.
_DATETIME_REVERSE_RE = re.compile(
    r"([0-9OoIl]{1,2}[:.h][0-9OoIl]{2}(?:[:.h][0-9OoIl]{2})?)"
    r"\s*"
    r"([0-9OoIl]{2}[\-—–/][0-9OoIl]{1,2}[\-—–/][0-9OoIl]{1,2})"
)


class BankilyExtractor(BaseExtractor):
    name = "bankily"

    def detect(self, text: str) -> float:
        score = 0.0
        markers = (
            (r"Paiement\s+r[eé]uss", 0.4),
            (r"Payment\s+Successful", 0.4),
            (r"Transfert\s+r[eé]uss", 0.35),  # transfert P2P (paiement valide)
            (r"Trs\s*ID", 0.35),
            (r"Txn\s*ID", 0.35),
            (r"PATRINET\s*NKTT", 0.2),
            (r"Effectu[eé]", 0.15),
            (r"\bDone\b", 0.1),
            # Marqueurs ajoutés pour notifications iOS / lignes d'historique :
            (r"\bBankily\b", 0.3),
            (r"\b016456\b", 0.25),
            (r"BKL[\s\-]?Paiement", 0.4),
            # Bankily ligne historique compacte (ex : "Paiement commercant 01-05-26
            # Montant:1000.0MRU Commercant:PATRINET 1226050100003140758").
            (r"Paiement\s+commer[cç]ant", 0.3),
            # Bankily P2P en arabe : OCR perd presque tout sauf un Trs ID 19 chiffres
            # qui commence par 0X (où X 1-9). Pattern très spécifique aux Trs ID
            # Bankily (peu de risque de faux positif).
            (r"\b0[1-9]\d{17}\b", 0.3),
            # Marchands alternatifs vus en Bankily (HOTEL HAYATT, etc.) :
            (r"\b01\d{4}\b", 0.15),  # codes marchands Bankily 6 chiffres "01XXXX"
        )
        for pattern, weight in markers:
            if re.search(pattern, text, re.IGNORECASE):
                score += weight
        return min(score, 1.0)

    # Seuil pour les fallbacks Txn ID. La regex `\d{15,22}` est trop large
    # et matcherait n'importe quel long numéro (Trs ID parasite, numéro de
    # série, etc.) sur du texte non-Bankily. On exige donc un détect ≥ 0.15
    # (le marqueur `01XXXX` à lui seul = 0.15, le P2P arabe = 0.3, etc.).
    _TXN_FALLBACK_THRESHOLD = 0.15

    def extract(self, text: str, ocr_result=None) -> ExtractionResult:
        result = ExtractionResult(template=self.name, detect_score=self.detect(text))
        ext = result.extracted

        # 1) Montant : ancre stricte → fallback MRU/nb → P2P arabe.
        # Pas de gating ici : si Bankily est le template choisi par le
        # dispatcher, c'est qu'il a le meilleur score → on lui fait confiance
        # pour tenter d'extraire le montant.
        m = _AMOUNT_RE.search(text) or _AMOUNT_FALLBACK_RE.search(text)
        if m:
            raw_amount = m.group(1) or m.group(2)
            ext.montant = normalize_amount(raw_amount)
            result.field_confidence["montant"] = self._confidence(ocr_result, raw_amount)
        else:
            m = _AMOUNT_P2P_RE.search(text)
            if m:
                raw = m.group(1) or m.group(2)
                ext.montant = normalize_amount(raw)
                result.field_confidence["montant"] = self._confidence(ocr_result, raw)

        # 2) Txn ID : ancre explicite > fallback 15-22 chiffres (gated).
        m = _TXN_RE.search(text)
        if m:
            ext.txn_id = normalize_txn_id(m.group(1))
            result.field_confidence["txn_id"] = self._confidence(ocr_result, m.group(1))
        elif result.detect_score >= self._TXN_FALLBACK_THRESHOLD:
            m = _TXN_FALLBACK_RE.search(text)
            if m:
                ext.txn_id = normalize_txn_id(m.group(1))
                result.field_confidence["txn_id"] = self._confidence(ocr_result, m.group(1))

        date_iso, date_raw = _find_date(text)
        if date_iso is not None:
            ext.date_heure = date_iso
            result.field_confidence["date_heure"] = self._confidence(ocr_result, date_raw)
        elif date_raw:
            ext.date_heure = normalize_datetime(date_raw)
            if ext.date_heure is not None:
                result.field_confidence["date_heure"] = self._confidence(ocr_result, date_raw)

        return result


_DATE_ONLY_RE = re.compile(
    r"(?<!\d)(\d{1,2}[\-—/]\d{1,2}[\-—/]\d{2}(?:\d{2})?)(?!\d)"
)


def _find_date(text: str):
    """Retourne (iso_or_None, raw_string).

    - Si `iso_or_None` est non None, c'est une ISO déjà construite (cas où
      le contexte est non ambigu, ex : layout RTL arabe = YY-MM-DD).
    - Sinon, `raw_string` est passé à `normalize_datetime` (cas ambigu où
      on s'en remet à la heuristique min(abs(year-today))).
    """
    anchor = _DATE_ANCHOR_RE.search(text)
    if anchor:
        tail = text[anchor.end(): anchor.end() + 60]
        m = _DATETIME_RE.search(tail)
        if m:
            return None, f"{m.group(1)} {m.group(2)}"

    m = _DATETIME_RE.search(text)
    if m:
        return None, f"{m.group(1)} {m.group(2)}"

    m = _DATETIME_REVERSE_RE.search(text)
    if m:
        # Layout Bankily arabe (RTL) : on SAIT que la date est en YY-MM-DD
        # car cette regex ne matche que dans ce contexte (HH:MM:SS suivi
        # d'une date). Pré-formater en ISO évite l'heuristique ambiguë.
        iso = parse_yymmdd_explicit(m.group(2), m.group(1))
        if iso is not None:
            return iso, f"{m.group(2)} {m.group(1)}"
        return None, f"{m.group(2)} {m.group(1)}"

    # Dernier recours : date seule sans heure
    m = _DATE_ONLY_RE.search(text)
    if m:
        return None, m.group(1)

    return None, None
