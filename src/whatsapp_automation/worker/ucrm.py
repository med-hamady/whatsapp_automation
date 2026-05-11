"""Client UCRM (vraie API en prod, fake en test).

Endpoints utilisés :
- GET  /clients/{id}/balance
- POST /payments
"""

from __future__ import annotations

import httpx

from .. import config


class UcrmError(Exception):
    pass


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "X-Auth-App-Key": config.UCRM_APP_KEY,
    }


async def get_balance(client_id: int) -> int:
    url = f"{config.UCRM_BASE_URL}/clients/{client_id}/balance"
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        r = await client.get(url, headers=_headers())
        r.raise_for_status()
        data = r.json()
        return int(data.get("balance", 0))


async def create_payment(client_id: int, amount: int, note: str | None = None) -> str:
    """Crée le paiement côté UCRM. Retourne l'id (string ou int converti)."""
    url = f"{config.UCRM_BASE_URL}/payments"
    payload = {"clientId": client_id, "amount": amount, "note": note}
    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
        r = await client.post(url, headers=_headers(), json=payload)
        if r.status_code >= 400:
            raise UcrmError(f"UCRM payment failed: {r.status_code} {r.text[:200]}")
        data = r.json()
        return str(data.get("id"))
