"""Dispatcher des extracteurs : choisit le template le mieux noté."""

from __future__ import annotations

from typing import List, Optional

from .base import BaseExtractor, ExtractionResult
from .bankily import BankilyExtractor
from .sedad import SedadExtractor
from .masrvi import MasrviExtractor
from .generic import GenericExtractor
from .subscription_form import SubscriptionFormExtractor


_EXTRACTORS: List[BaseExtractor] = [
    # En tête : écarte les fiches "NOUVEL ABONNEMENT" avant qu'un extracteur
    # de reçu (Sedad sur "non-paiement") ne les capte par erreur.
    SubscriptionFormExtractor(),
    BankilyExtractor(),
    SedadExtractor(),
    MasrviExtractor(),
    GenericExtractor(),
]


def extract(text: str, ocr_result=None) -> ExtractionResult:
    best_extractor: Optional[BaseExtractor] = None
    best_score = -1.0
    for extractor in _EXTRACTORS:
        score = extractor.detect(text)
        if score > best_score:
            best_score = score
            best_extractor = extractor
    if best_extractor is None or best_score < 0.05:
        best_extractor = _EXTRACTORS[-1]
    return best_extractor.extract(text, ocr_result)


__all__ = ["extract", "ExtractionResult"]
