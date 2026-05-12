"""Client UCRM (vraie API en prod, fake en test).

UCRM expose deux APIs distinctes sur le même hôte :

- Billing  : POST {UCRM_BASE_URL}/api/v1.0/payments
            auth = header ``X-Auth-App-Key`` (clé applicative).
- CRM      : GET  {UCRM_BASE_URL}/crm/api/v1.0/clients/{id}
            auth = header ``x-auth-token`` (UUID).

Le solde dû est lu sur le champ ``accountOutstanding`` du client.
La création de paiement retourne ``paymentCovers[0].paymentId`` — c'est cette
valeur qui est stockée en DB locale et qui pilote l'URL du PDF.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .. import config


class UcrmError(Exception):
    pass


def _billing_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "X-Auth-App-Key": config.UCRM_APP_KEY,
    }


def _crm_headers() -> dict:
    return {
        "Accept": "application/json",
        "x-auth-token": config.UCRM_CRM_TOKEN,
    }


async def get_balance(client_id: int) -> int:
    """Retourne ``accountOutstanding`` du client (montant dû, en MRU).

    Tape sur l'API CRM (préfixe ``/crm/api/v1.0/``) avec le token UUID.
    """
    url = f"{config.UCRM_BASE_URL}/crm/api/v1.0/clients/{client_id}"
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        r = await client.get(url, headers=_crm_headers())
        r.raise_for_status()
        data = r.json() or {}
        return int(float(data.get("accountOutstanding", 0) or 0))


async def create_payment(client_id: int, amount: int, note: str | None = None) -> str:
    """Crée le paiement côté UCRM Billing. Retourne le ``paymentId``.

    Le payload réplique exactement celui de l'ancien `UcrmApiAccess_pay::doRequest`
    PHP (currencyCode, methodId, userId, applyToInvoicesAutomatically=True…).
    L'identifiant retourné est ``paymentCovers[0].paymentId``.
    """
    url = f"{config.UCRM_BASE_URL}/api/v1.0/payments"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    iso = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    payload = {
        "currencyCode": config.UCRM_CURRENCY,
        "attributes": [],
        "applyToInvoicesAutomatically": True,
        "invoiceIds": [],
        "clientId": int(client_id),
        "methodId": config.UCRM_METHOD_ID,
        "checkNumber": "",
        "createdDate": iso,
        "amount": int(amount),
        "note": note or "Whatsapp",
        "providerName": "",
        "providerPaymentId": "",
        "providerPaymentTime": iso,
        "userId": config.UCRM_USER_ID,
    }
    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
        r = await client.post(url, headers=_billing_headers(), json=payload)
        if r.status_code >= 400:
            raise UcrmError(f"UCRM payment failed: {r.status_code} {r.text[:200]}")
        data = r.json() or {}

    covers = data.get("paymentCovers") or []
    if covers and isinstance(covers, list) and covers[0].get("paymentId") is not None:
        return str(covers[0]["paymentId"])
    # Filet de sécurité : certains environnements (paiement non rattaché à une
    # facture) renvoient directement l'objet Payment sans paymentCovers.
    if data.get("id") is not None:
        return str(data["id"])
    raise UcrmError(f"UCRM payment response missing paymentId: {data}")
