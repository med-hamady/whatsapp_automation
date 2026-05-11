"""Fake UCRM — simule l'API REST UCRM (https://13.62.145.152/api/v1.0).

Endpoints reproduits :
- GET  /clients/{id}/balance  → solde dû du client
- POST /payments              → création d'un paiement, retourne {id}

Lancement :
    python -m fakes.fake_ucrm
ou via run_fakes.bat (port 9001).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel


app = FastAPI(title="fake-ucrm", version="0.1.0")

# Soldes dûs par client (mock)
BALANCES = {
    1: 1500,
    2: 1190,
    3: 990,
    4: 0,        # client à jour
    5: 1000,     # Client Test 48783201
}

# Compteur paiements
_payment_counter = itertools.count(start=1000)

# Liste des paiements créés (visible via GET /payments)
PAYMENTS: list[dict] = []


class PaymentIn(BaseModel):
    clientId: int
    amount: int
    note: str | None = None


def _check_auth(x_auth_app_key: str | None) -> None:
    """Vérifie la clé d'auth. En vrai UCRM utilise X-Auth-App-Key."""
    if not x_auth_app_key:
        raise HTTPException(status_code=401, detail="missing_auth_key")


@app.get("/clients/{client_id}/balance")
def get_balance(client_id: int, x_auth_app_key: str | None = Header(default=None)):
    _check_auth(x_auth_app_key)
    if client_id not in BALANCES:
        raise HTTPException(status_code=404, detail="client_not_found")
    return {"clientId": client_id, "balance": BALANCES[client_id]}


@app.post("/payments")
def create_payment(payment: PaymentIn, x_auth_app_key: str | None = Header(default=None)):
    _check_auth(x_auth_app_key)
    payment_id = next(_payment_counter)
    record = {
        "id": payment_id,
        "clientId": payment.clientId,
        "amount": payment.amount,
        "note": payment.note,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    PAYMENTS.append(record)
    return record


@app.get("/payments")
def list_payments(x_auth_app_key: str | None = Header(default=None)):
    _check_auth(x_auth_app_key)
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
