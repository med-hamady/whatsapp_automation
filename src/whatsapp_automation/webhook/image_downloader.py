"""Téléchargement de l'image depuis l'URL fournie par UltraMsg (typiquement S3)."""

from __future__ import annotations

import logging
from typing import Optional

import httpx


logger = logging.getLogger("whatsapp_automation.webhook.image")

MAX_BYTES = 8 * 1024 * 1024  # 8 Mo


async def download(url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            content = response.content
            if len(content) > MAX_BYTES:
                logger.warning("image trop grande (%d bytes), drop", len(content))
                return None
            return content
    except httpx.HTTPError as exc:
        logger.error("téléchargement image échoué: %s", exc)
        return None
