"""Routes FastAPI du dashboard de supervision.

- `GET  /dashboard`           page HTML (login si non authentifié, sinon le tableau)
- `POST /dashboard/login`     {password} → pose le cookie de session
- `GET  /dashboard/logout`    efface le cookie
- `GET  /dashboard/api/*`     données JSON (protégées par require_session)

Les pages HTML sont servies telles quelles (assets CSS/JS inline) ; toute la
donnée est récupérée par le front via les endpoints /api/*.
"""

from __future__ import annotations

import logging
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from ... import config
from ...db import postgres as pg
from ...jobqueue import store as queue_store
from . import auth, events_db, samples

logger = logging.getLogger("whatsapp_automation.webhook.dashboard")

router = APIRouter()

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")


def _render(name: str) -> str:
    with open(os.path.join(_TEMPLATES_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


@router.get("/dashboard/logo.png")
def dashboard_logo():
    """Logo A2 Holding (public : affiché aussi sur la page de login)."""
    return FileResponse(os.path.join(_ASSETS_DIR, "a2_logo.png"), media_type="image/png")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    if not auth.is_authenticated(request):
        return HTMLResponse(_render("login.html"))
    return HTMLResponse(_render("dashboard.html"))


class LoginBody(BaseModel):
    password: str = ""


@router.post("/dashboard/login")
def dashboard_login(body: LoginBody):
    if not auth.check_password(body.password):
        return JSONResponse({"ok": False}, status_code=401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        auth.SESSION_COOKIE,
        auth.make_token(),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


@router.get("/dashboard/logout")
def dashboard_logout():
    resp = Response(status_code=204)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@router.get("/dashboard/api/summary", dependencies=[Depends(auth.require_session)])
def api_summary(days: int = Query(30, ge=1, le=365)):
    data = events_db.summary(days=days)
    data["queue"] = queue_store.stats()
    try:
        data["paiment_total"] = pg.count_paiements()
    except Exception as exc:  # DB indispo → on n'invalide pas tout le dashboard
        logger.warning("dashboard: count_paiements KO: %s", exc)
        data["paiment_total"] = None
    return data


@router.get("/dashboard/api/refusals", dependencies=[Depends(auth.require_session)])
def api_refusals(days: int = Query(30, ge=1, le=365)):
    return events_db.refusals_by_cause(days=days)


@router.get("/dashboard/api/timeseries", dependencies=[Depends(auth.require_session)])
def api_timeseries(days: int = Query(30, ge=1, le=365)):
    return events_db.timeseries(days=days)


@router.get("/dashboard/api/events", dependencies=[Depends(auth.require_session)])
def api_events(
    days: int = Query(30, ge=1, le=3650),
    limit: int = Query(150, ge=1, le=1000),
    type: str = Query("", alias="type"),
    q: str = Query("", description="Recherche libre (client, téléphone, txn, paiement, montant)"),
):
    return events_db.recent_events(limit=limit, type_filter=type or None, days=days, q=q or None)


@router.get("/dashboard/api/event_detail", dependencies=[Depends(auth.require_session)])
def api_event_detail(
    txn_id: str = Query("", description="Transaction ID du reçu"),
    client_id: int = Query(0, description="ID client (0 = inconnu)"),
    date: str = Query("", description="Date de l'événement YYYY-MM-DD (indice de recherche)"),
    payment_id: str = Query("", description="paymentId UCRM (pour le reçu envoyé)"),
    phone: str = Query("", description="Téléphone (repli historique si client_id inconnu)"),
    amount: int = Query(0, description="Montant de l'événement (résolution txn manquant)"),
):
    """Détail d'un événement :
    - capture OCR (par txn_id) du reçu reçu du client ;
    - reçu ENVOYÉ au client (texte exact + PDF) reconstitué par payment_id ;
    - historique des paiements du client.
    """
    rows = _client_paiements(client_id, phone)

    # Événements sans txn dans le log (ex : sous-paiement) : on retrouve le
    # paiement correspondant (même client, même montant, même jour) pour
    # récupérer son txn_id (→ capture) et son id_payment (→ reçu + statut).
    if not txn_id and amount and date and rows:
        match = next(
            (r for r in rows if r.get("amount") == amount and _row_date(r) == date),
            None,
        )
        if match:
            txn_id = match.get("txn_id") or ""
            if not payment_id and match.get("id_payment") is not None:
                payment_id = str(match["id_payment"])

    sample = samples.find_sample_by_txn(txn_id, date or None) if txn_id else None
    receipt = _build_receipt(payment_id) if payment_id else None

    # Si on n'avait ni client_id ni téléphone (ex : reçu envoyé), on récupère
    # l'idclient du job reconstitué pour quand même afficher l'historique.
    if not rows and receipt and receipt.get("client_id"):
        rows = _client_paiements(receipt["client_id"], "")

    payments = [{
        "id_payment": r.get("id_payment"),
        "amount": r.get("amount"),
        "date": _row_date(r),
        "txn_id": r.get("txn_id"),
    } for r in rows]

    return {"sample": sample or {"found": False}, "receipt": receipt, "payments": payments}


def _row_date(r: dict) -> str:
    return f"{r['year']:04d}-{r['month']:02d}-{r['day']:02d}"


def _client_paiements(client_id: int, phone: str) -> list[dict]:
    try:
        if client_id:
            return pg.get_paiements_by_client(client_id)
        if phone:
            return pg.get_paiements_by_phone(phone)
    except Exception as exc:
        logger.warning("dashboard: historique paiements KO (client=%s phone=%s): %s",
                       client_id, phone, exc)
    return []


def _build_receipt(payment_id: str) -> dict:
    """Reconstitue le reçu envoyé au client : texte du message + URL du PDF.

    Le PDF est toujours disponible (URL déterministe par payment_id). Le texte
    exact n'est reconstituable que si le job est encore en queue (payload).
    """
    pdf_url = config.PDF_URL_TEMPLATE.format(payment_id=payment_id)
    receipt = {"found": False, "pdf_url": pdf_url, "payment_id": payment_id, "caption": None}
    job = queue_store.get_job_by_payment_id(payment_id)
    if job is not None:
        try:
            # Réutilise la fonction de PROD qui a construit le message envoyé,
            # pour un rendu identique au caption WhatsApp réel.
            from ...worker.handlers import _build_message_body
            receipt["caption"] = _build_message_body(job)
            receipt["found"] = True
            receipt["client_id"] = job.client.id
            receipt["amount"] = job.payment.amount_mru
            receipt["balance_before"] = job.payment.crm_balance_before
            receipt["unblocked"] = job.payment.should_unblock
            # Statut de l'abonnement avant ce paiement (capturé par le webhook)
            # et après (le worker réactive si should_unblock). "suspended"/"active".
            before = job.client.current_status
            receipt["status_before"] = before
            receipt["status_after"] = "active" if job.payment.should_unblock else before
        except Exception as exc:
            logger.warning("dashboard: reconstitution caption KO (pid=%s): %s", payment_id, exc)
    return receipt


@router.get("/dashboard/api/client", dependencies=[Depends(auth.require_session)])
def api_client(id: int = Query(..., ge=1, description="ID client")):
    """Fiche client par ID : infos + abonnements + paiements + tous ses événements."""
    try:
        rows = pg.get_client_by_id(id)
    except Exception as exc:
        logger.warning("dashboard: get_client_by_id(%s) KO: %s", id, exc)
        rows = []
    paiements = _client_paiements(id, "")

    # Téléphones connus du client (depuis `info` et la table paiment) pour
    # rattacher aussi les événements qui ne portent que le numéro.
    phones: set[str] = set()
    for r in rows:
        m = re.match(r"\s*(\d{6,})", r.get("info") or "")
        if m:
            phones.add(m.group(1))
    for p in paiements:
        if p.get("phone"):
            phones.add(str(p["phone"]))

    events = events_db.events_for_client(id, list(phones))

    # Résumé client (nom/téléphone parsés depuis `info` = "<phone>-<nom>").
    name = phone = None
    if rows:
        info = rows[0].get("info") or ""
        m = re.match(r"\s*(\d+)\s*[-:_ ]\s*(.*)", info)
        if m:
            phone, name = m.group(1), (m.group(2).strip() or None)
        else:
            name = info or None
    statu_vals = [r.get("statu") for r in rows]
    status = "suspended" if any(s == 2 for s in statu_vals) else ("active" if rows else None)

    return {
        "found": bool(rows or paiements or events),
        "client": {
            "id": id,
            "name": name,
            "phone": phone or (sorted(phones)[0] if phones else None),
            "status": status,
            "subscriptions": [
                {"mac": r.get("mac"), "statu": r.get("statu"), "ip": r.get("ipaddress")}
                for r in rows
            ],
            "payments_count": len(paiements),
            "total_paid": sum(int(p.get("amount") or 0) for p in paiements),
        },
        "payments": [{
            "id_payment": p.get("id_payment"), "amount": p.get("amount"),
            "date": _row_date(p), "txn_id": p.get("txn_id"),
        } for p in paiements],
        "events": events,
    }


@router.get("/dashboard/api/sample_image", dependencies=[Depends(auth.require_session)])
def api_sample_image(
    date: str = Query(..., description="YYYY-MM-DD"),
    sid: str = Query(..., description="hex32 du sample"),
):
    """Sert l'image (capture) d'un reçu archivé. Chemin validé (anti-traversal)."""
    path = samples.image_path(date, sid)
    if not path:
        raise HTTPException(status_code=404, detail="image_not_found")
    return FileResponse(path, media_type="image/jpeg")
