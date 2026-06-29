"""Client UltraMsg : envoi du PDF de reçu au client par WhatsApp."""

from __future__ import annotations

import httpx

from .. import config


class UltraMsgError(Exception):
    pass


async def send_document(to: str, document_url: str, filename: str, caption: str | None = None) -> dict:
    url = f"{config.ULTRAMSG_BASE_URL}/{config.ULTRAMSG_INSTANCE}/messages/document"
    payload = {
        "token": config.ULTRAMSG_TOKEN,
        "to": to,
        "filename": filename,
        "document": document_url,
        "caption": caption or "",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=payload)
        if r.status_code >= 400:
            raise UltraMsgError(f"UltraMsg failed: {r.status_code} {r.text[:200]}")
        return r.json()


async def send_chat(to: str, body: str) -> dict:
    url = f"{config.ULTRAMSG_BASE_URL}/{config.ULTRAMSG_INSTANCE}/messages/chat"
    payload = {
        "token": config.ULTRAMSG_TOKEN,
        "to": to,
        "body": body,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=payload)
        if r.status_code >= 400:
            raise UltraMsgError(f"UltraMsg failed: {r.status_code} {r.text[:200]}")
        return r.json()


async def send_image(to: str, image_url: str, caption: str | None = None) -> dict:
    """Envoie une image via UltraMsg en pointant sur une URL publique.

    Utilisé pour reforwarder la capture du reçu d'un client introuvable dans
    le CRM au numéro support : l'URL `media` reçue d'UltraMsg dans le webhook
    est accessible publiquement et peut être directement réenvoyée.
    """
    url = f"{config.ULTRAMSG_BASE_URL}/{config.ULTRAMSG_INSTANCE}/messages/image"
    payload = {
        "token": config.ULTRAMSG_TOKEN,
        "to": to,
        "image": image_url,
        "caption": caption or "",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, json=payload)
        if r.status_code >= 400:
            raise UltraMsgError(f"UltraMsg failed: {r.status_code} {r.text[:200]}")
        return r.json()
