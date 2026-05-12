"""Fake UCRM — simule les deux APIs UCRM réelles.

Endpoints reproduits (préfixes calqués sur la prod) :
- GET  /crm/api/v1.0/clients/{id}  → renvoie ``accountOutstanding`` (auth: x-auth-token)
- POST /api/v1.0/payments          → renvoie ``paymentCovers[0].paymentId`` (auth: X-Auth-App-Key)

Endpoints debug (pratiques pour les demos) :
- GET  /payments                   → liste les paiements créés
- GET  /health

Lancement :
    python -m fakes.fake_ucrm
ou via run_fakes.bat (port 9001).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel


app = FastAPI(title="fake-ucrm", version="0.2.0")

# Soldes dûs par client (mock) — alignés sur seed.sql (9 clients suspendus).
BALANCES = {
    1: 900,    # 48783201
    2: 1000,   # 48249066
    3: 1190,   # 46603985
    4: 1500,   # 31752614
    5: 990,    # 37888210
    6: 850,    # 44160960
    7: 1200,   # 777565497
    8: 1100,   # 33848414
    9: 950,    # 41769945
}

_payment_counter = itertools.count(start=1000)
PAYMENTS: list[dict] = []


class PaymentIn(BaseModel):
    # Réplique du payload PHP UcrmApiAccess_pay. Tous les champs sont acceptés
    # mais on ne valide que ceux dont la valeur sert au mock.
    clientId: int
    amount: int
    note: str | None = None
    currencyCode: str | None = None
    methodId: str | None = None
    userId: int | None = None
    applyToInvoicesAutomatically: bool | None = None
    invoiceIds: list[Any] | None = None
    attributes: list[Any] | None = None
    checkNumber: str | None = None
    createdDate: str | None = None
    providerName: str | None = None
    providerPaymentId: str | None = None
    providerPaymentTime: str | None = None


def _check_billing_auth(x_auth_app_key: str | None) -> None:
    if not x_auth_app_key:
        raise HTTPException(status_code=401, detail="missing_app_key")


def _check_crm_auth(x_auth_token: str | None) -> None:
    if not x_auth_token:
        raise HTTPException(status_code=401, detail="missing_crm_token")


@app.get("/crm/api/v1.0/clients/{client_id}")
def get_client(client_id: int, x_auth_token: str | None = Header(default=None)):
    _check_crm_auth(x_auth_token)
    if client_id not in BALANCES:
        raise HTTPException(status_code=404, detail="client_not_found")
    return {
        "id": client_id,
        "accountOutstanding": BALANCES[client_id],
        "accountCredit": 0,
        "currencyCode": "MRU",
    }


@app.post("/api/v1.0/payments")
def create_payment(payment: PaymentIn, x_auth_app_key: str | None = Header(default=None)):
    _check_billing_auth(x_auth_app_key)
    payment_id = next(_payment_counter)
    record = {
        "id": payment_id,
        "clientId": payment.clientId,
        "amount": payment.amount,
        "note": payment.note,
        "methodId": payment.methodId,
        "userId": payment.userId,
        "currencyCode": payment.currencyCode,
        "createdDate": payment.createdDate
            or datetime.now(timezone.utc).isoformat(),
    }
    PAYMENTS.append(record)
    # UCRM réel renvoie un objet Payment enrichi d'un tableau paymentCovers
    # (une entrée par facture couverte). On en met une seule, qui pointe sur
    # le paymentId — c'est cette valeur que lit `setPayment` côté PHP/Python.
    return {
        **record,
        "paymentCovers": [
            {"paymentId": payment_id, "invoiceId": None, "amount": payment.amount}
        ],
    }


@app.get("/payments")
def list_payments(x_auth_app_key: str | None = Header(default=None)):
    _check_billing_auth(x_auth_app_key)
    return PAYMENTS


@app.get("/health")
def health():
    return {"ok": True, "service": "fake-ucrm", "payments_count": len(PAYMENTS)}


def main():
    import uvicorn

    uvicorn.run(
        "fakes.fake_ucrm:app",
        host="127.0.0.1",
        port=9001,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
