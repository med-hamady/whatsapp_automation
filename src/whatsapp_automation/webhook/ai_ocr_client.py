"""Client HTTP du service ai_ocr local (port 8008)."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from .. import config


logger = logging.getLogger("whatsapp_automation.webhook.ai_ocr")


async def extract(image_bytes: bytes, filename: str = "receipt.jpg") -> Optional[dict]:
    """Appelle POST /extract du service ai_ocr. Retourne le dict JSON, ou None
    en cas d'erreur (qu'on traitera comme "non extractible").

    On distingue deux familles d'erreur :
    - "rejected" (4xx) : l'OCR est joignable mais a refusé l'image (format
      invalide, vide, trop grosse). C'est un cas attendu pour les médias
      non-images qu'on n'aurait pas filtrés en amont.
    - "unreachable" (réseau / 5xx) : l'OCR est down ou planté. Alerte plus
      sérieuse.
    """
    url = f"{config.AI_OCR_URL}/extract"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            files = {"file": (filename, image_bytes, "image/jpeg")}
            response = await client.post(url, files=files)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        detail = ""
        try:
            detail = exc.response.json().get("detail", "")
        except Exception:
            detail = exc.response.text[:200]
        if 400 <= status < 500:
            logger.info("ai_ocr rejected (HTTP %d): %s", status, detail)
        else:
            logger.error("ai_ocr error (HTTP %d): %s", status, detail)
        return None
    except httpx.HTTPError as exc:
        logger.error("ai_ocr unreachable: %s", exc)
        return None
