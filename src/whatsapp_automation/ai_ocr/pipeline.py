"""Pipeline complet OCR + extraction + 2e passe optionnelle.

Centralise la logique de traitement pour qu'elle soit identique entre :
- le service FastAPI (`/extract`)
- le batch d'ingestion (`scripts/batch_ingest.py`)
- la ré-extraction (`scripts/reextract.py`)

La 2e passe à contraste boosté est déclenchée pour tout résultat où le
montant est manquant ET le 1er passage suggère un Sedad (template choisi
ou score Sedad significatif). Le cas qu'on cherche à rattraper : montant
noyé dans une phrase arabe RTL fragmentant la lecture.
"""

from __future__ import annotations

from . import engine as ocr_engine
from .extractors import _EXTRACTORS
from .extractors import extract as run_extractors
from .extractors.sedad import SedadExtractor


# Score Sedad minimum à partir duquel on tente la 2e passe même si un autre
# template a été choisi (ex : Sedad mal classé en "generic" sur OCR très
# bruité).
_SEDAD_HINT_THRESHOLD = 0.3


def _looks_like_sedad(text: str, template: str) -> bool:
    if template == "sedad":
        return True
    sedad = next((e for e in _EXTRACTORS if isinstance(e, SedadExtractor)), None)
    if sedad is None:
        return False
    return sedad.detect(text) >= _SEDAD_HINT_THRESHOLD


def process_image(image_bytes: bytes) -> dict:
    """OCR + extraction (+ 2e passe si nécessaire). Retourne le payload.

    Champs retournés (alignés avec le format de prediction.json) :
        extracted, confidence, template, raw_text
    """
    ocr_result = ocr_engine.run_ocr(image_bytes)
    extraction = run_extractors(ocr_result.text, ocr_result)

    # 2e passe contraste boosté quand le montant manque ET le texte ressemble
    # à un Sedad. On garde la passe qui retourne le résultat le plus complet.
    if extraction.extracted.montant is None and _looks_like_sedad(
        ocr_result.text, extraction.template
    ):
        ocr2 = ocr_engine.run_ocr_high_contrast(image_bytes)
        extraction2 = run_extractors(ocr2.text, ocr2)
        if extraction2.extracted.montant is not None:
            ocr_result = ocr2
            extraction = extraction2

    return {
        "ocr_result": ocr_result,
        "extraction": extraction,
        "prediction": {
            "extracted": extraction.extracted.to_dict(),
            "confidence": {
                **extraction.field_confidence,
                "overall": extraction.overall_confidence(),
            },
            "template": extraction.template,
            "raw_text": ocr_result.text,
        },
    }
