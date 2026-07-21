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
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from ... import config
from ...db import postgres as pg
from ...jobqueue import store as queue_store
from .. import job_builder
from ..validators import validate_crm_balance, validate_payment_balance
from . import auth, events_db, samples, unknown_clients_store

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
def api_summary(
    days: int = Query(30, ge=1, le=3650),
    start: str = Query("", description="Date de début YYYY-MM-DD (prioritaire sur days)"),
    end: str = Query("", description="Date de fin YYYY-MM-DD"),
):
    data = events_db.summary(days=days, start=start or None, end=end or None)
    data["queue"] = queue_store.stats()
    try:
        data["paiment_total"] = pg.count_paiements()
    except Exception as exc:  # DB indispo → on n'invalide pas tout le dashboard
        logger.warning("dashboard: count_paiements KO: %s", exc)
        data["paiment_total"] = None
    return data


@router.get("/dashboard/api/refusals", dependencies=[Depends(auth.require_session)])
def api_refusals(
    days: int = Query(30, ge=1, le=3650),
    start: str = Query(""),
    end: str = Query(""),
):
    return events_db.refusals_by_cause(days=days, start=start or None, end=end or None)


@router.get("/dashboard/api/timeseries", dependencies=[Depends(auth.require_session)])
def api_timeseries(
    days: int = Query(30, ge=1, le=3650),
    start: str = Query(""),
    end: str = Query(""),
):
    return events_db.timeseries(days=days, start=start or None, end=end or None)


@router.get("/dashboard/api/events", dependencies=[Depends(auth.require_session)])
def api_events(
    days: int = Query(30, ge=1, le=3650),
    limit: int = Query(150, ge=1, le=1000),
    type: str = Query("", alias="type"),
    q: str = Query("", description="Recherche libre (client, téléphone, txn, paiement, montant)"),
    start: str = Query(""),
    end: str = Query(""),
):
    return events_db.recent_events(
        limit=limit, type_filter=type or None, days=days, q=q or None,
        start=start or None, end=end or None,
    )


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
    rows, _ = _client_paiements(client_id, phone)

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
        rows, _ = _client_paiements(receipt["client_id"], "")

    payments = [{
        "id_payment": r.get("id_payment"),
        "amount": r.get("amount"),
        "date": _row_date(r),
        "txn_id": r.get("txn_id"),
    } for r in rows]

    return {"sample": sample or {"found": False}, "receipt": receipt, "payments": payments}


def _row_date(r: dict) -> str:
    return f"{r['year']:04d}-{r['month']:02d}-{r['day']:02d}"


def _client_paiements(client_id: int, phone: str) -> tuple[list[dict], bool]:
    """Retourne (lignes, db_error). db_error=True signale une panne de
    connexion PostgreSQL (ex : pg_hba.conf), jamais confondue avec "aucun
    paiement trouvé" (liste vide, db_error=False)."""
    try:
        if client_id:
            return pg.get_paiements_by_client(client_id), False
        if phone:
            return pg.get_paiements_by_phone(phone), False
    except Exception as exc:
        logger.warning("dashboard: historique paiements KO (client=%s phone=%s): %s",
                       client_id, phone, exc)
        return [], True
    return [], False


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
    db_error = False
    try:
        rows = pg.get_client_by_id(id)
    except Exception as exc:
        logger.warning("dashboard: get_client_by_id(%s) KO: %s", id, exc)
        rows = []
        db_error = True
    paiements, paiements_error = _client_paiements(id, "")
    db_error = db_error or paiements_error

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
        "db_error": db_error,
        "client": {
            "id": id,
            "name": name,
            "phone": phone or (sorted(phones)[0] if phones else None),
            "status": status,
            "subscriptions": [
                {"mac": r.get("mac"), "statu": r.get("statu"), "ip": r.get("ipaddress"),
                 "info": r.get("info")}
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


@router.get("/dashboard/api/unknown-clients", dependencies=[Depends(auth.require_session)])
def api_unknown_clients(
    limit: int = Query(50, ge=1, le=500),
    status: str = Query("", description="Filtre statut (pending, ...). Vide = tous."),
):
    """Paiements reçus d'un numéro non rattaché à un client (table dédiée
    `numeros_introuvable`). Lecture seule, aucun appel externe."""
    return unknown_clients_store.list_recent(limit=limit, status=status or None)


@router.get(
    "/dashboard/api/unknown-clients/{id}", dependencies=[Depends(auth.require_session)]
)
def api_unknown_client_detail(id: int):
    """Détail d'un enregistrement `numeros_introuvable`. Ajoute `sample_sid`
    (hex du sample OCR, dérivé de sample_id) pour que le front puisse charger
    l'image via /dashboard/api/sample_image?date=...&sid=..., sans appel externe."""
    row = unknown_clients_store.get_by_id(id)
    if not row:
        raise HTTPException(status_code=404, detail="not_found")
    if row.get("sample_id") and "/" in row["sample_id"]:
        row["sample_sid"] = row["sample_id"].split("/", 1)[1]
    return row


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


class AssociateBody(BaseModel):
    # int accepté aussi (un client HTTP peut envoyer 48213 sans guillemets) ;
    # normalisé/validé dans la route.
    crm_client_id: int | str = ""


def _name_from_info(info: Optional[str]) -> Optional[str]:
    """Nom du client extrait du champ `info` ("<phone>-<nom>"), même parsing
    que le résumé client de api_client ci-dessus."""
    m = re.match(r"\s*(\d+)\s*[-:_ ]\s*(.*)", info or "")
    if m:
        return m.group(2).strip() or None
    return (info or "").strip() or None


def _status_label(statu) -> Optional[str]:
    return "suspended" if statu == 2 else ("active" if statu == 0 else None)


@router.post(
    "/dashboard/api/unknown-clients/{id}/associate",
    dependencies=[Depends(auth.require_session)],
)
def api_unknown_client_associate(id: int, body: AssociateBody):
    """Associe un enregistrement `numeros_introuvable` au client PostgreSQL
    identifié par son IDENTIFIANT CRM saisi par l'admin dans le modal du
    dashboard.

    L'identifiant CRM (idclient) est exact là où une recherche par téléphone
    (`info LIKE %phone%`) serait ambiguë. Le lookup passe par
    pg.get_client_by_id — jamais get_clients_by_phone ici.

    Lecture seule sur PostgreSQL (aucune écriture). Écrit uniquement le
    `client_id` sur le ticket `numeros_introuvable` (le `whatsapp_phone` est
    déjà connu depuis sa création). Cette association seule n'influence PAS
    encore le routage des paiements futurs de ce numéro : tant que le ticket
    n'est pas confirmé (statut 'queued'), `find_client_id_for_phone` l'ignore
    — une association abandonnée ou erronée (mauvais identifiant CRM saisi,
    jamais confirmé) ne doit jamais router silencieusement un paiement futur
    vers le mauvais client. Ne crée AUCUN paiement, AUCUN Job de file
    d'attente, et n'appelle ni UCRM, ni MikroTik, ni UltraMsg.
    """
    row = unknown_clients_store.get_by_id(id)
    if not row:
        raise HTTPException(status_code=404, detail="not_found")

    # Ré-association autorisée tant qu'aucune confirmation n'a démarré
    # (corriger un mauvais identifiant CRM saisi) ; refusée dès 'confirming'
    # ou 'queued' pour ne jamais changer le client d'un paiement déjà engagé.
    if row["status"] not in ("pending", "associated"):
        raise HTTPException(
            status_code=409,
            detail=f"Statut invalide pour association : {row['status']}",
        )

    # txn_id absent accepté : certains opérateurs (masrivi, generic) n'ont
    # structurellement pas de txn_id extractible (cf. jobqueue/schema.sql) et
    # sont pourtant traités normalement par le flux webhook — un ticket
    # `numeros_introuvable` ne doit pas être plus restrictif que lui.

    raw = str(body.crm_client_id).strip()
    if not raw.isdigit() or int(raw) <= 0:
        raise HTTPException(
            status_code=400,
            detail="Identifiant CRM invalide. Entier positif attendu (ex : 48213).",
        )
    crm_client_id = int(raw)

    try:
        client_rows = pg.get_client_by_id(crm_client_id)
    except Exception as exc:
        logger.warning("dashboard: associate get_client_by_id KO (id=%s): %s", id, exc)
        raise HTTPException(status_code=502, detail="Base clients PostgreSQL indisponible.")

    if not client_rows:
        unknown_clients_store.mark_unknown_client_error(
            id, f"Aucun client PostgreSQL trouvé pour l'identifiant CRM {crm_client_id}",
        )
        raise HTTPException(status_code=404, detail="Aucun client trouvé pour cet identifiant CRM.")

    # Aperçu complet : toutes les lignes/abonnements du client. Purement
    # informatif — la confirmation relira PostgreSQL/UCRM à chaud avant de
    # construire le Job (jamais ces valeurs figées).
    first = client_rows[0]
    client_id = str(crm_client_id)
    subscriptions = [
        {
            "mac": r.get("mac"),
            "ip": r.get("ipaddress"),
            "statu": r.get("statu"),
            "status": _status_label(r.get("statu")),
            "info": r.get("info"),
        }
        for r in client_rows
    ]

    updated = unknown_clients_store.associate_unknown_client(id, client_id=client_id)
    if not updated:
        raise HTTPException(status_code=500, detail="Échec de l'association (SQLite).")

    return {
        "ok": True,
        "status": "associated",
        "unknown_client": updated,
        "client_preview": {
            "client_id": client_id,
            "name": _name_from_info(first.get("info")),
            "status": _status_label(first.get("statu")),
            "ip_address": first.get("ipaddress"),
            "rows_count": len(client_rows),
            "subscriptions_count": len(client_rows),
            "subscriptions": subscriptions,
        },
        "message": "Client associé. Aucun paiement n'a été créé.",
    }


class _ConfirmFailure(Exception):
    """Échec récupérable pendant la confirmation : la réservation
    'confirming' doit être relâchée vers 'associated' avant de répondre à
    l'appelant (aucun Job n'a été construit/empilé)."""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _reconcile_via_existing_job(id: int, row: dict) -> Optional[dict]:
    """Si un Job existe déjà en queue pour le txn_id de cet enregistrement
    (actif ou terminé), le rattache et termine la confirmation. Ne crée
    jamais de second Job. Retourne la réponse de succès, ou None si aucun Job
    n'est trouvé (l'appelant doit alors décider quoi faire : la confirmation
    est peut-être encore légitimement en cours, cf. `_recover_stale_or_reject`)."""
    txn_id = row.get("txn_id")
    if not txn_id:
        return None
    job_info = queue_store.find_job_by_txn(txn_id)
    if not job_info:
        return None
    if not unknown_clients_store.mark_queued(id, job_info["job_id"]):
        return None
    return {
        "ok": True,
        "status": "queued",
        "job_id": job_info["job_id"],
        "reconciled": True,
        "message": "Confirmation récupérée après incident : le paiement était déjà en file.",
    }


def _recover_stale_or_reject(id: int, row: dict) -> None:
    """Un enregistrement 'confirming' sans Job retrouvé en queue (cf.
    `_reconcile_via_existing_job`) est soit une confirmation concurrente
    réellement en cours (relectures PostgreSQL/UCRM en vol — de vraies I/O
    réseau, pas juste quelques microsecondes), soit un reliquat abandonné
    (process tué, ou exception non gérée, survenu APRÈS
    `reserve_for_confirmation()` mais AVANT `queue_store.enqueue()`).

    On ne peut distinguer les deux qu'avec un délai : `updated_at` (posé par
    `reserve_for_confirmation`) sert d'horodatage de réservation.
    - Plus jeune que `config.UNKNOWN_CLIENT_CONFIRM_TIMEOUT_SECONDS` : on
      considère qu'une requête sœur est peut-être toujours active — on ne
      touche JAMAIS l'enregistrement, on répond juste "réessayez".
    - Plus vieux que ce délai : `release_stale_confirmation` tente une
      restauration atomique confirming->associated (CAS incluant la
      condition d'âge dans le même UPDATE, donc deux récupérations
      concurrentes sur le même enregistrement stale ne peuvent pas toutes les
      deux réussir).

    Lève toujours HTTPException (jamais de retour) : ni le cas frais ni le
    cas stale-récupéré n'aboutissent à une confirmation terminée dans CET
    appel — l'admin doit relancer une nouvelle confirmation ensuite."""
    age_seconds = time.time() - float(row.get("updated_at") or 0)
    timeout = config.UNKNOWN_CLIENT_CONFIRM_TIMEOUT_SECONDS

    if age_seconds < timeout:
        raise HTTPException(
            status_code=409,
            detail="Une confirmation est déjà en cours pour cet enregistrement. Réessayez dans quelques secondes.",
        )

    error_message = (
        f"Confirmation expirée après {timeout}s sans Job retrouvé en queue — "
        "remise en 'associated', vous pouvez réessayer."
    )
    released = unknown_clients_store.release_stale_confirmation(
        id, error_message, min_age_seconds=timeout,
    )
    if released:
        raise HTTPException(status_code=409, detail=error_message)

    # rowcount == 0 : une autre requête a déjà transitionné cet enregistrement
    # entre notre lecture initiale et cette tentative (soit elle l'a
    # réconcilié vers 'queued', soit elle a gagné la course de restauration
    # stale en premier). On ne réécrit rien à l'aveugle — juste 409 générique,
    # cohérent dans les deux cas (le prochain appel de l'admin verra l'état à
    # jour, que ce soit 'queued' ou 'associated').
    raise HTTPException(
        status_code=409,
        detail="Une confirmation est déjà en cours pour cet enregistrement. Réessayez dans quelques secondes.",
    )


@router.post(
    "/dashboard/api/unknown-clients/{id}/confirm",
    dependencies=[Depends(auth.require_session)],
)
async def api_unknown_client_confirm(id: int):
    """Confirme un ticket 'associated' et empile un Job de paiement complet
    en queue SQLite.

    Ne fait JAMAIS : UCRM create_payment, MikroTik unblock, UltraMsg send,
    écriture PostgreSQL — ces actions restent strictement réservées au
    worker, qui consommera le Job une fois en file. Cette route ne fait que
    relire PostgreSQL/UCRM à l'instant présent (jamais les valeurs figées de
    l'association) et appeler `queue_store.enqueue()` exactement une fois.

    Concurrence/crash : `reserve_for_confirmation` (CAS associated->confirming)
    garantit qu'une seule confirmation avance à la fois pour cet
    enregistrement. Si `enqueue()` renvoie None (dédup atomique par txn_id) ou
    si un appel précédent a crashé entre l'enqueue et le mark_queued, on
    inspecte la queue par txn_id et on rattache le Job existant plutôt que
    d'en recréer un — jamais deux Jobs pour le même reçu. Si un enregistrement
    reste bloqué en 'confirming' sans Job (crash — ou exception non gérée —
    survenu avant même l'enqueue), il n'est récupéré vers 'associated' qu'après
    `config.UNKNOWN_CLIENT_CONFIRM_TIMEOUT_SECONDS` d'inactivité (cf.
    `_recover_stale_or_reject`), pour ne jamais couper une confirmation sœur
    encore légitimement en cours (relectures PostgreSQL/UCRM).
    """
    row = unknown_clients_store.get_by_id(id)
    if not row:
        raise HTTPException(status_code=404, detail="not_found")

    if row["status"] == "queued":
        # Idempotent : une confirmation déjà aboutie ne recrée jamais de Job.
        return {
            "ok": True,
            "status": "queued",
            "job_id": row.get("job_id"),
            "message": "Paiement déjà mis en file.",
        }

    if row["status"] == "confirming":
        recovered = _reconcile_via_existing_job(id, row)
        if recovered is not None:
            return recovered
        _recover_stale_or_reject(id, row)  # lève toujours HTTPException

    if row["status"] != "associated":
        raise HTTPException(
            status_code=409, detail=f"Statut invalide pour confirmation : {row['status']}",
        )

    # Préconditions strictes — refusées AVANT toute réservation (aucun état
    # SQLite touché si l'une d'elles échoue). txn_id absent accepté (cf.
    # gate retiré dans associate() : certains opérateurs n'en ont jamais).
    amount = row.get("amount")
    if not amount or amount <= 0:
        raise HTTPException(status_code=409, detail="Montant manquant ou invalide.")
    if not row.get("whatsapp_phone"):
        raise HTTPException(status_code=409, detail="whatsapp_phone manquant.")
    if not row.get("client_id"):
        raise HTTPException(status_code=409, detail="client_id manquant (association requise).")
    if row.get("job_id"):
        raise HTTPException(status_code=409, detail="Déjà rattaché à un job existant.")

    if not unknown_clients_store.reserve_for_confirmation(id):
        # Course perdue contre une autre confirmation concurrente sur ce même id.
        raise HTTPException(
            status_code=409,
            detail="Une autre confirmation est déjà en cours pour cet enregistrement.",
        )

    try:
        try:
            client_id_int = int(row["client_id"])
        except (TypeError, ValueError):
            raise _ConfirmFailure(f"client_id invalide: {row['client_id']!r}")

        # Relecture PostgreSQL fraîche — jamais des valeurs figées à
        # l'association. Un client peut avoir plusieurs abonnements :
        # get_client_by_id retourne déjà TOUTES les lignes de cet idclient.
        try:
            client_rows = pg.get_client_by_id(client_id_int)
        except Exception as exc:
            raise _ConfirmFailure(f"PostgreSQL indisponible : {exc}"[:200], status_code=502)

        if not client_rows:
            raise _ConfirmFailure(
                f"Client PostgreSQL introuvable (idclient={client_id_int}).", status_code=404,
            )
        client_row = client_rows[0]

        # Relecture UCRM fraîche (helper partagé webhook/dashboard, avec retry).
        details, services = await job_builder.fetch_ucrm_context(client_id_int)
        crm_balance = int(details.get("balance") or 0) if details else None

        valid_balance = validate_crm_balance(crm_balance)
        if not valid_balance.ok:
            raise _ConfirmFailure("UCRM injoignable.", status_code=502)

        amount_paid = int(amount)
        valid_overpay = validate_payment_balance(
            amount_paid=amount_paid, crm_balance=crm_balance,
            threshold=config.UNDERPAYMENT_TOLERANCE,
        )
        if not valid_overpay.ok:
            raise _ConfirmFailure(f"Paiement refusé : {valid_overpay.reason}")

        decision = job_builder.compute_unblock_plan(
            client_row=client_row, client_rows=client_rows, details=details, services=services,
            amount_paid=amount_paid, crm_balance=crm_balance,
            threshold=config.UNDERPAYMENT_TOLERANCE,
        )

        whatsapp_phone = row["whatsapp_phone"]
        job = job_builder.build_job(
            client_row=client_row,
            amount_paid=amount_paid,
            txn_id=row["txn_id"],
            date_heure=row.get("date_heure"),
            template=row.get("operator") or "generic",
            crm_balance=crm_balance,
            unblock_macs=decision.unblock_macs,
            # Règle métier : le reçu final part TOUJOURS au numéro qui a
            # envoyé le paiement (whatsapp_phone) — client.phone ET
            # source.wnum, contrairement au flux webhook où ils peuvent
            # différer (cf. job_builder.build_job).
            phone_for_worker=whatsapp_phone,
            wnum=whatsapp_phone,
            sample_id=row.get("sample_id") or "",
        )
    except _ConfirmFailure as fail:
        unknown_clients_store.release_confirmation(id, fail.message)
        raise HTTPException(status_code=fail.status_code, detail=fail.message)
    except Exception as exc:
        logger.exception("dashboard confirm id=%s : échec inattendu avant enqueue", id)
        unknown_clients_store.release_confirmation(id, f"{type(exc).__name__}: {exc}"[:200])
        raise HTTPException(status_code=500, detail="Échec interne, réessayez.")

    internal_id = queue_store.enqueue(job)
    if internal_id is None:
        # Doublon détecté par l'index UNIQUE partiel de la queue (même txn_id
        # déjà pending/processing/retry, ou déjà processed_payments). On
        # inspecte la queue AVANT de décider : si un Job actif/terminé existe
        # déjà pour ce txn_id, on le rattache (jamais un 2e Job) ; sinon on
        # restaure 'associated' et on renvoie 409 (cas très improbable —
        # `reserve_for_confirmation` protège déjà contre ce scénario pour CE
        # même enregistrement, mais un autre enregistrement `numeros_introuvable`
        # avec le même txn_id, ou un paiement déjà traité par le webhook,
        # restent possibles).
        job_info = queue_store.find_job_by_txn(job.payment.txn_id)
        if job_info:
            unknown_clients_store.mark_queued(id, job_info["job_id"])
            return {
                "ok": True,
                "status": "queued",
                "job_id": job_info["job_id"],
                "reconciled": True,
                "message": "Paiement déjà en file (rattaché à un job existant).",
            }
        unknown_clients_store.release_confirmation(
            id, "Doublon détecté à l'enqueue mais introuvable en queue.",
        )
        raise HTTPException(status_code=409, detail="Doublon détecté, réessayez.")

    unknown_clients_store.mark_queued(id, job.job_id)
    logger.info(
        "dashboard confirm id=%s job_id=%s client=%d txn=%s amount=%d unblock_macs=%s",
        id, job.job_id, job.client.id, job.payment.txn_id, job.payment.amount_mru, job.unblock_macs,
    )
    return {
        "ok": True,
        "status": "queued",
        "job_id": job.job_id,
        "client_id": job.client.id,
        "amount_paid": job.payment.amount_mru,
        "crm_balance": crm_balance,
        "should_unblock": job.payment.should_unblock,
        "unblock_macs": job.unblock_macs,
        "message": "Paiement mis en file. Le worker le traitera prochainement.",
    }
