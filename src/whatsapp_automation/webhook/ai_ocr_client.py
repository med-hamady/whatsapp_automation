"""Client HTTP du service ai_ocr local (port 8008)."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from .. import config


logger = logging.getLogger("whatsapp_automation.webhook.ai_ocr")


async def extract(image_bytes: bytes, filename: str = "receipt.jpg") -> Optional[dict]:
    """Appelle POST /extract du service ai_ocr. Retourne le dict JSON, ou None
    en cas d'erreur (qu'on traitera comme "non extractible")."""
    url = f"{config.AI_OCR_URL}/extract"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            files = {"file": (filename, image_bytes, "image/jpeg")}
            response = await client.post(url, files=files)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        logger.error("ai_ocr unreachable: %s", exc)
        return None
