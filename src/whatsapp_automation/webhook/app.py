"""FastAPI app du webhook. Endpoint :
   POST /webhook  — reçoit UltraMsg, répond 200 OK en < 100 ms.

La logique métier est offload-ée dans une tâche asyncio détachée pour ne
pas bloquer la réponse à UltraMsg (qui sinon timeout et rejoue le message).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel

from .. import __version__, config
from ..db import postgres as pg
from ..jobqueue import store as queue_store
from ..worker import fai_supervisor, mikrotik, ucrm
from . import pipeline
from .dashboard import router as dashboard_router
from .dashboard import events_db, unknown_clients_store
from .phone import parse_from_field


# Configure le logging applicatif au niveau INFO, sortie stdout + fichier.
# uvicorn --log-level info ne configure QUE ses propres loggers ; sans cette
# ligne, les `logger.info(...)` de l'app sont muets (root = WARNING).
# `force=True` pour réécraser une éventuelle config posée par uvicorn.
import os as _os
from logging.handlers import RotatingFileHandler as _RFH
_LOG_DIR = _os.path.join(_os.getcwd(), "data", "logs")
_os.makedirs(_LOG_DIR, exist_ok=True)
_fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
_file_handler = _RFH(
    _os.path.join(_LOG_DIR, "webhook.log"),
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setFormatter(_fmt)
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_fmt)
logging.basicConfig(
    level=logging.INFO,
    handlers=[_file_handler, _stream_handler],
    force=True,
)

logger = logging.getLogger("whatsapp_automation.webhook")


# Intervalle de ré-ingestion des logs → table events (secondes).
EVENTS_INGEST_INTERVAL = 60.0


async def _events_ingest_loop(stop: asyncio.Event):
    """Alimente périodiquement la table events depuis les logs (idempotent).

    La 1re itération (au démarrage) fait le backfill complet. L'ingestion lit
    ~dizaines de Mo de logs : on l'exécute dans un thread pour ne pas bloquer
    l'event loop, et on espace les passages (INSERT OR IGNORE = dédup)."""
    loop = asyncio.get_event_loop()
    while not stop.is_set():
        try:
            n = await loop.run_in_executor(None, events_db.ingest_from_logs)
            if n:
                logger.info("events ingestion: +%d (total=%d)", n, events_db.count())
        except Exception:
            logger.exception("events ingestion failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=EVENTS_INGEST_INTERVAL)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    queue_store.init_db()
    events_db.init_db()
    unknown_clients_store.init_db()
    stop = asyncio.Event()
    ingest_task = asyncio.create_task(_events_ingest_loop(stop))
    try:
        yield
    finally:
        stop.set()
        ingest_task.cancel()
        try:
            await ingest_task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(title="whatsapp_automation.webhook", version=__version__, lifespan=lifespan)

# Dashboard de supervision (lecture seule) : /dashboard + /dashboard/api/*.
app.include_router(dashboard_router)


@app.get("/health")
def health():
    return {"ok": True, "service": "webhook", "version": __version__, "queue": queue_store.stats()}


@app.post("/webhook")
async def webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "reason": "invalid_json"}

    # Détache le traitement — UltraMsg reçoit 200 immédiatement.
    asyncio.create_task(_safe_process(payload))
    return {"ok": True}


async def _safe_process(payload: dict):
    try:
        result = await pipeline.process(payload)
        logger.info("pipeline result: %s", result)
    except Exception:
        logger.exception("pipeline crashed")


@app.post("/webhook/sync")
async def webhook_sync(request: Request):
    """Variante SYNCHRONE pour tests : on attend la fin du pipeline et on
    renvoie son résumé. NE PAS pointer UltraMsg dessus en prod."""
    payload = await request.json()
    result = await pipeline.process(payload)
    return result


@app.get("/queue/stats")
def queue_stats():
    return queue_store.stats()


async def _safe_call(coro):
    """Isole un appel async externe : exception → {data: None, error: msg}.

    Permet à /api/clients/lookup de renvoyer une réponse partielle quand
    UCRM ou Mikrotik est injoignable, plutôt que 500 sur tout l'endpoint.
    """
    try:
        return {"data": await coro, "error": None}
    except Exception as exc:
        return {"data": None, "error": f"{type(exc).__name__}: {exc}"[:200]}


async def _device_status(row: dict) -> dict:
    """État réseau d'un abonnement/équipement local (1 ligne `client`).

    Combine les infos locales (mac, ip, statu) avec l'état de blocage des DEUX
    mécanismes de coupure : le firewall MikroTik et le superviseur LR. Un client
    peut être coupé par l'un sans l'autre, donc ``is_blocked`` est vrai dès que
    l'un des deux le bloque ; ``blocked_mikrotik`` / ``blocked_supervisor``
    détaillent lequel.

    Les MAC placeholder ``pending-XXXX`` (client sans MAC réel en UCRM) et les
    MAC vides ne déclenchent aucun appel externe — is_blocked reste False.
    """
    mac = (row.get("mac") or "").strip()
    base = {
        "mac": mac,
        "ip": row.get("ipaddress"),
        "statu_local": row.get("statu"),
    }
    if not mac or mac.lower().startswith("pending-"):
        return {
            **base,
            "is_blocked": False,
            "block_rule_count": 0,
            "blocked_mikrotik": False,
            "blocked_supervisor": False,
            "block_mode": None,
            "error": None,
        }

    mk_res, sup_res = await asyncio.gather(
        _safe_call(mikrotik.get_block_status_by_mac(mac)),
        _fai_status(mac),
    )
    block = mk_res["data"] or {}
    sup = sup_res["data"] or {}

    blocked_mk = block.get("is_blocked")
    blocked_sup = sup.get("client_blocked")
    errors = [e for e in (mk_res["error"], sup_res["error"]) if e]

    return {
        **base,
        "is_blocked": bool(blocked_mk) or bool(blocked_sup),
        "block_rule_count": block.get("block_rule_count"),
        "blocked_mikrotik": blocked_mk,
        "blocked_supervisor": blocked_sup,
        "block_mode": sup.get("block_mode"),
        "error": "; ".join(errors) if errors else None,
    }


async def _fai_status(mac: str):
    """État superviseur d'une MAC, jamais bloquant.

    Superviseur non configuré → ``{data: None}`` sans appel réseau : la réponse
    du lookup reste alors celle d'avant (MikroTik seul).
    """
    if not fai_supervisor.enabled():
        return {"data": None, "error": None}
    return await _safe_call(fai_supervisor.get_status_by_mac(mac))


@app.get("/api/clients/lookup")
async def lookup_client(
    phone: str = Query(..., min_length=1, description="Numéro de téléphone du client"),
    x_api_key: str = Header(default="", alias="X-API-Key"),
):
    """Consultation client par téléphone — agrège DB locale + UCRM + Mikrotik.

    Lecture seule. Auth obligatoire via header X-API-Key (sinon 401).
    Réponse partielle (champ ``null`` + détail dans ``errors``) si une source
    externe est injoignable — l'endpoint ne renvoie 500 que sur bug interne.

    La DB locale sert à résoudre téléphone → idclient + MAC, mais elle n'est PAS
    la source de vérité de l'existence d'un client : si elle ne connaît pas le
    numéro, on retombe sur une recherche CRM (``found: true``, ``fai: []``).
    """
    if not config.CLIENT_API_KEY or x_api_key != config.CLIENT_API_KEY:
        raise HTTPException(status_code=401, detail="invalid_api_key")

    norm = parse_from_field(phone) or phone

    # La DB locale résout le téléphone → idclient + tous les MAC du client.
    # Un client peut avoir plusieurs abonnements (plusieurs lignes / MAC), d'où
    # get_clients_by_phone (toutes les lignes) et pas get_client_by_phone (1re).
    locals_ = pg.get_clients_by_phone(norm)

    if locals_:
        # Les lignes d'un même client partagent l'idclient ; il sert pour UCRM.
        idclient = int(locals_[0]["idclient"])
        erreur_repli = None
    else:
        # Repli CRM : la DB locale n'est qu'un miroir (alimenté par la synchro),
        # elle peut ignorer un client tout juste créé dans UCRM. Sans ce repli,
        # un vrai client resterait introuvable tant qu'il n'est pas synchronisé.
        # On interroge donc le CRM directement. `locals_` reste vide : le client
        # n'a alors aucun équipement connu → `fai: []`, pas d'état Mikrotik.
        repli = await _safe_call(ucrm.find_client_id_by_phone(norm))
        idclient = repli["data"]
        erreur_repli = repli["error"]
        if idclient is None:
            return {
                "phone": norm,
                "found": False,
                "crm": None,
                "services_count": None,
                "services": None,
                "recent_invoices": None,
                "fai_count": None,
                "fai": None,
                # `crm_lookup` distingue « le CRM ne le connaît pas non plus »
                # (null) d'une panne du CRM pendant le repli (message d'erreur).
                "errors": {"local": "not_found", "crm_lookup": erreur_repli},
            }

    # CRM (détails), services (forfaits), factures, et l'état Mikrotik de CHAQUE
    # MAC, tout en parallèle. _device_status isole déjà ses erreurs (jamais 500).
    crm_res, services_res, invoices_res, *fai_list = await asyncio.gather(
        _safe_call(ucrm.get_client_details(idclient)),
        _safe_call(ucrm.get_client_services(idclient)),
        _safe_call(ucrm.get_client_invoices(idclient, limit=5)),
        *[_device_status(row) for row in locals_],
    )

    services_data = services_res["data"]
    fai_errors = [d["error"] for d in fai_list if d.get("error")]
    return {
        "phone": norm,
        "found": True,
        "crm": crm_res["data"],
        "services_count": len(services_data) if services_data is not None else None,
        "services": services_data,
        "recent_invoices": invoices_res["data"],
        "fai_count": len(fai_list),
        "fai": fai_list,
        "errors": {
            "crm": crm_res["error"],
            "services": services_res["error"],
            "invoices": invoices_res["error"],
            "fai": fai_errors[0] if fai_errors else None,
        },
    }


class BlockRequest(BaseModel):
    phone: str
    mac: str
    action: str  # "block" | "unblock"


@app.post("/api/clients/block")
async def block_client(
    body: BlockRequest,
    x_admin_key: str = Header(default="", alias="X-Admin-Key"),
):
    """Bloque ou débloque un abonnement (MAC) d'un client sur le réseau.

    Action d'écriture protégée par une clé ADMIN distincte (header X-Admin-Key).
    Le MAC doit appartenir au téléphone fourni (validation anti-abus).

    Le BLOCAGE ne concerne que le firewall MikroTik : couper un client est la
    prérogative du superviseur réseau, pas du système de paiement. Le DEBLOCAGE,
    lui, lève les deux coupures possibles (MikroTik + superviseur LR), puisqu'un
    client a pu être coupé par l'un ou par l'autre.

    MikroTik reste bloquant (échec → 502, le statut local n'est pas modifié) ; le
    superviseur, lui, n'est pas bloquant : son résultat (ou son erreur) est
    rapporté dans ``supervisor``, car un 404 (MAC hors parc supervisé) ou un 409
    (LR en bridge) ne doivent pas annuler le déblocage MikroTik déjà appliqué.
    """
    if not config.ADMIN_API_KEY or x_admin_key != config.ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="invalid_admin_key")

    action = (body.action or "").strip().lower()
    if action not in ("block", "unblock"):
        raise HTTPException(status_code=422, detail="action must be 'block' or 'unblock'")

    mac_in = (body.mac or "").strip()
    if not mac_in:
        raise HTTPException(status_code=422, detail="mac required")

    norm = parse_from_field(body.phone) or body.phone

    # Sécurité : le MAC doit être un des abonnements rattachés à ce téléphone.
    locals_ = pg.get_clients_by_phone(norm)
    match = next(
        (r for r in locals_ if (r.get("mac") or "").lower() == mac_in.lower()),
        None,
    )
    if match is None:
        raise HTTPException(status_code=404, detail="mac_not_found_for_phone")

    # On utilise la MAC telle que stockée en base (casse exacte) pour l'UPDATE.
    db_mac = match["mac"]
    new_statu = 2 if action == "block" else 0

    try:
        if action == "block":
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            comment = f"API block {match.get('info', '')} {ts}"
            rules_changed = await mikrotik.block_by_mac(db_mac, comment)
        else:
            rules_changed = await mikrotik.unblock_by_mac(db_mac)
    except Exception as exc:
        # Le routeur a échoué : on ne touche pas au statut local (pas d'incohérence).
        logger.exception("block_client mikrotik %s failed for mac=%s", action, db_mac)
        raise HTTPException(
            status_code=502,
            detail=f"mikrotik_error: {type(exc).__name__}: {exc}"[:200],
        )

    # Déblocage seulement : le système de paiement ne pose jamais de coupure
    # sur le superviseur — c'est la prérogative de l'équipe réseau.
    supervisor = await _fai_unblock(db_mac) if action == "unblock" else None

    rows = pg.update_client_status_by_mac(db_mac, new_statu)
    status_after = await mikrotik.get_block_status_by_mac(db_mac)

    logger.info(
        "block_client OK phone=%s mac=%s action=%s rules_changed=%d statu=%d "
        "supervisor_ok=%s supervisor_err=%s",
        norm, db_mac, action, rules_changed, new_statu,
        (supervisor or {}).get("ok"), (supervisor or {}).get("error"),
    )
    return {
        "phone": norm,
        "mac": db_mac,
        "action": action,
        "rules_changed": rules_changed,
        "statu_local": new_statu,
        "local_rows_updated": rows,
        "is_blocked": status_after.get("is_blocked"),
        "block_rule_count": status_after.get("block_rule_count"),
        "supervisor": supervisor,
    }


async def _fai_unblock(mac: str) -> dict | None:
    """Lève la coupure superviseur pour cette MAC, sans jamais lever d'exception.

    Retourne ``None`` si le superviseur n'est pas configuré, sinon sa réponse
    enrichie d'un champ ``error`` (``None`` si tout s'est bien passé).

    Rappel : ``ok: false`` n'est PAS un échec — le LR est injoignable à cet
    instant, l'ordre est enregistré et ré-appliqué automatiquement. C'est
    ``client_blocked`` qui porte l'intention.
    """
    if not fai_supervisor.enabled():
        return None
    try:
        return {**await fai_supervisor.unblock_by_mac(mac), "error": None}
    except fai_supervisor.FaiSupervisorError as exc:
        logger.error("block_client superviseur unblock ECHEC mac=%s http=%s err=%s",
                     mac, exc.status_code, exc)
        return {"ok": False, "http_status": exc.status_code, "error": str(exc)[:200]}
