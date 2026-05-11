"""Interface commune des extracteurs par template."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Extracted:
    montant: Optional[int] = None
    txn_id: Optional[str] = None
    date_heure: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "montant": self.montant,
            "txn_id": self.txn_id,
            "date_heure": self.date_heure,
        }


@dataclass
class ExtractionResult:
    template: str
    extracted: Extracted = field(default_factory=Extracted)
    detect_score: float = 0.0
    field_confidence: dict = field(default_factory=dict)

    def overall_confidence(self) -> float:
        weights = {"montant": 0.5, "txn_id": 0.3, "date_heure": 0.2}
        score = 0.0
        total_weight = 0.0
        for field_name, weight in weights.items():
            if getattr(self.extracted, field_name) is not None:
                score += self.field_confidence.get(field_name, 0.0) * weight
                total_weight += weight
        if total_weight == 0:
            return 0.0
        return score / total_weight


class BaseExtractor:
    name: str = "base"

    def detect(self, text: str) -> float:
        """Retourne un score 0..1 de correspondance au template."""
        raise NotImplementedError

    def extract(self, text: str, ocr_result=None) -> ExtractionResult:
        raise NotImplementedError

    def _confidence(self, ocr_result, value: Optional[str]) -> float:
        """Confiance OCR pour la valeur extraite. Fallback 0.6 si OCR absent."""
        if value is None:
            return 0.0
        if ocr_result is None:
            return 0.6
        from whatsapp_automation.ai_ocr.engine import confidence_for_span

        return confidence_for_span(ocr_result, str(value)) or 0.6
