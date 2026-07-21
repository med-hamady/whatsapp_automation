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


async def get_client_details(client_id: int) -> dict:
    """Retourne les détails utiles d'un client UCRM (lecture seule).

    Tape sur le même endpoint CRM que ``get_balance``. Le payload UCRM est
    riche ; on ne remonte qu'un sous-ensemble explicite (identité + indicateurs
    de compte) demandé par l'endpoint /api/clients/lookup. ``balance`` est un
    int en MRU (alias entier d'accountOutstanding) — kept for backward compat
    avec get_balance ; les trois valeurs flottantes accountBalance / Credit /
    Outstanding viennent telles quelles de UCRM.
    """
    url = f"{config.UCRM_BASE_URL}/crm/api/v1.0/clients/{client_id}"
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        r = await client.get(url, headers=_crm_headers())
        r.raise_for_status()
        data = r.json() or {}

    contacts = data.get("contacts") or []
    first_contact = contacts[0] if contacts else {}

    return {
        "id": data.get("id"),
        "client_type": data.get("clientType"),
        "first_name": data.get("firstName"),
        "last_name": data.get("lastName"),
        "phone": first_contact.get("phone"),
        "balance": int(float(data.get("accountOutstanding", 0) or 0)),
        "registration_date": data.get("registrationDate"),
        "is_active": data.get("isActive"),
        "account_balance": data.get("accountBalance"),
        "account_credit": data.get("accountCredit"),
        "account_outstanding": data.get("accountOutstanding"),
        "has_suspended_service": data.get("hasSuspendedService"),
    }


def _chiffres(valeur: str | None) -> str:
    """Réduit un numéro à ses chiffres, sans l'indicatif 222 (comme phone.py)."""
    digits = "".join(c for c in str(valeur or "") if c.isdigit())
    if digits.startswith("222") and len(digits) > 8:
        return digits[3:]
    return digits


async def find_client_id_by_phone(phone: str) -> int | None:
    """Cherche un client UCRM par téléphone et retourne son id, ou None.

    Sert de repli à /api/clients/lookup quand la DB locale ne connaît pas encore
    le numéro (client créé dans le CRM mais pas encore synchronisé localement).

    ``query`` est un recherche plein-texte UCRM : elle peut matcher sur le nom
    ou l'adresse autant que sur le téléphone. On re-vérifie donc côté Python que
    l'un des contacts porte bien ce numéro, pour ne jamais retourner un client
    dont le seul tort est d'avoir ces chiffres dans son adresse.
    """
    cible = _chiffres(phone)
    if not cible:
        return None

    url = f"{config.UCRM_BASE_URL}/crm/api/v1.0/clients"
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        r = await client.get(url, headers=_crm_headers(), params={"query": cible})
        r.raise_for_status()
        resultats = r.json() or []

    for candidat in resultats:
        for contact in candidat.get("contacts") or []:
            if _chiffres(contact.get("phone")) == cible:
                return int(candidat["id"])
    return None


_UCRM_SERVICE_STATUS = {
    0: "Prepared", 1: "Active", 2: "Ended", 3: "Suspended",
    4: "Cancelled", 5: "Quoted", 6: "Inactive", 7: "Obsolete",
    8: "Deferred",
}


async def get_client_services(client_id: int) -> list[dict]:
    """Retourne la liste des services (forfaits) d'un client UCRM.

    Un client peut avoir plusieurs services (internet + IPTV, etc.). Pour
    chacun on remonte nom, type, statut, prix, débit, dates clés. UCRM expose
    cela via /clients/services?clientId=X (note : pluriel /services au niveau
    racine, filtré par clientId — pas /clients/X/services).
    """
    url = f"{config.UCRM_BASE_URL}/crm/api/v1.0/clients/services"
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        r = await client.get(url, headers=_crm_headers(), params={"clientId": client_id})
        r.raise_for_status()
        data = r.json() or []

    return [
        {
            "id": s.get("id"),
            "name": s.get("name") or s.get("servicePlanName"),
            "type": s.get("servicePlanType"),
            "status": s.get("status"),
            "status_label": _UCRM_SERVICE_STATUS.get(s.get("status"), None) if s.get("status") is not None else None,
            "price": s.get("totalPrice") if s.get("totalPrice") is not None else s.get("price"),
            "mac": s.get("macAddress"),
            "ip": s.get("ip"),
            "currency": s.get("currencyCode"),
            "download_speed_mb": s.get("downloadSpeed"),
            "upload_speed_mb": s.get("uploadSpeed"),
            "active_from": s.get("activeFrom"),
            "active_to": s.get("activeTo"),
            "last_invoiced_date": s.get("lastInvoicedDate"),
            "prepaid": s.get("prepaid"),
            "has_outage": s.get("hasOutage"),
        }
        for s in data
    ]


_UCRM_INVOICE_STATUS = {
    0: "Draft", 1: "Unpaid", 2: "PartiallyPaid", 3: "Paid",
    4: "Void", 5: "ProcessedProforma",
}


async def get_client_invoices(client_id: int, limit: int = 5) -> list[dict]:
    """Retourne les N dernières factures d'un client UCRM (plus récentes d'abord).

    UCRM trie côté serveur via order=createdDate&direction=DESC, on s'appuie
    dessus (validé en prod) plutôt que de rapatrier tout l'historique.
    """
    url = f"{config.UCRM_BASE_URL}/crm/api/v1.0/invoices"
    params = {
        "clientId": client_id,
        "limit": limit,
        "order": "createdDate",
        "direction": "DESC",
    }
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        r = await client.get(url, headers=_crm_headers(), params=params)
        r.raise_for_status()
        data = r.json() or []

    return [
        {
            "id": inv.get("id"),
            "number": inv.get("number"),
            "created_date": inv.get("createdDate"),
            "due_date": inv.get("dueDate"),
            "total": inv.get("total"),
            "amount_paid": inv.get("amountPaid"),
            "amount_to_pay": inv.get("amountToPay"),
            "currency": inv.get("currencyCode"),
            "status": inv.get("status"),
            "status_label": _UCRM_INVOICE_STATUS.get(inv.get("status")) if inv.get("status") is not None else None,
        }
        for inv in data
    ]


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
