"""Pipeline du webhook : reçoit le payload UltraMsg, prépare TOUT, empile.

Étapes :
1. Parse le payload UltraMsg → URL image, numéro émetteur.
2. Télécharge l'image.
3. Appelle ai_ocr → {montant, txn_id, date_heure, ...}.
4. Idempotence : si txn_id déjà traité/en cours, on s'arrête.
5. Lookup client en DB locale.
6. Validations (statut suspendu, montant > 0).
7. Construit le Job complet et empile en queue SQLite.

Aucune des étapes 1-6 ne fait d'I/O bloquante sortante coûteuse à part le
téléchargement et l'appel ai_ocr. Le worker ne fait jamais ces lookups.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from .. import config
from ..models import Client, Job, Payment, Source
from ..jobqueue import store as queue_store
from ..db import postgres as pg
from ..worker import ucrm
from .ai_ocr_client import extract as ai_ocr_extract
from .image_downloader import download as download_image
from .phone import parse_body_number, parse_from_field
from .validators import (
    should_unblock_client,
    validate_client,
    validate_crm_balance,
    validate_extraction,
    validate_recipient_name,
)


logger = logging.getLogger("whatsapp_automation.webhook.pipeline")


# Backoff entre tentatives UCRM get_balance. 3 essais au total ; ~4s max
# d'attente cumulée avant d'abandonner. La requête tourne dans une asyncio
# Task détachée (cf. app._safe_process) donc on ne bloque pas UltraMsg.
UCRM_GET_BALANCE_DELAYS = (0, 1, 3)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


async def _get_balance_with_retry(client_id: int) -> Optional[int]:
    """Appelle ucrm.get_balance avec retry sur erreurs transitoires.

    Retourne None si toutes les tentatives échouent (timeout, réseau, 5xx)
    OU si UCRM renvoie une 4xx (erreur métier, inutile de retry).
    """
    last_exc: Optional[Exception] = None
    for attempt, delay in enumerate(UCRM_GET_BALANCE_DELAYS, 1):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await ucrm.get_balance(client_id)
        except httpx.HTTPStatusError as exc:
            # 4xx → erreur métier (client introuvable, token invalide…)
            # inutile de retry, on coupe court.
            if 400 <= exc.response.status_code < 500:
                logger.warning(
                    "UCRM get_balance client=%d HTTP %d : abandon (pas de retry)",
                    client_id, exc.response.status_code,
                )
                return None
            last_exc = exc
        except Exception as exc:
            last_exc = exc
        logger.warning(
            "UCRM get_balance tentative %d/%d KO client=%d : %s: %r",
            attempt, len(UCRM_GET_BALANCE_DELAYS), client_id,
            type(last_exc).__name__, last_exc,
        )
    return None


async def process(payload: dict) -> dict:
    """Traite un payload UltraMsg. Retourne un résumé (pour logs/debug ;
    UltraMsg n'utilise pas la réponse au-delà du status 200)."""

    event_data = (payload.get("data")
                  or payload.get("event", {}).get("data")
                  or {})
    from_phone = parse_from_field(event_data.get("from", ""))
    body_phone = parse_body_number(event_data.get("body", ""))
    media_url = event_data.get("media")
    msg_type = (event_data.get("type") or "").lower()

    # On ne traite que les images. UltraMsg envoie aussi des stickers, vidéos,
    # audio (voice notes), documents PDF — ils ont un `media` mais ne sont pas
    # des reçus de paiement. On les drop AVANT de télécharger pour éviter
    # bande passante inutile et un 400 systématique côté ai_ocr.
    if msg_type and msg_type != "image":
        logger.info("type=%s non supporté, drop (from=%s)", msg_type, from_phone)
        return {"status": "skipped", "reason": f"unsupported_type:{msg_type}"}

    if not media_url:
        logger.info("no media, drop (from=%s)", from_phone)
        return {"status": "skipped", "reason": "no_media"}

    image_bytes = await download_image(media_url)
    if image_bytes is None:
        return {"status": "skipped", "reason": "image_download_failed"}

    ocr_response = await ai_ocr_extract(image_bytes)
    if ocr_response is None:
        return {"status": "skipped", "reason": "ai_ocr_unreachable"}

    extracted = ocr_response.get("extracted") or {}
    sample_id = ocr_response.get("sample_id", "")
    template = ocr_response.get("template", "generic")
    raw_text = ocr_response.get("raw_text") or ""

    valid_ext = validate_extraction(extracted)
    if not valid_ext.ok:
        logger.info("extraction invalide: %s", valid_ext.reason)
        return {"status": "skipped", "reason": valid_ext.reason}

    # Anti-fraude : on n'accepte le paiement que si le nom du destinataire
    # PATRINET / A2 CONNECT / PATRIE NET apparaît dans la capture. Bloque
    # les reçus envoyés vers un autre compte.
    valid_recipient = validate_recipient_name(template, raw_text)
    if not valid_recipient.ok:
        logger.info(
            "destinataire KO: %s (template=%s, from=%s)",
            valid_recipient.reason, template, from_phone,
        )
        return {"status": "skipped", "reason": valid_recipient.reason}

    txn_id: Optional[str] = extracted.get("txn_id") or ""

    # Idempotence en 2 étages :
    #  1. processed_payments (job déjà terminé avec succès)
    #  2. jobs en cours (pending/processing/retry)
    if txn_id:
        if queue_store.is_txn_processed(txn_id):
            logger.info("idempotence: txn_id %s déjà traité avec succès", txn_id)
            return {"status": "skipped", "reason": "duplicate_processed"}
        if queue_store.is_txn_in_flight(txn_id):
            logger.info("idempotence: txn_id %s déjà en queue", txn_id)
            return {"status": "skipped", "reason": "duplicate_in_flight"}

    # Lookup client (DB locale) — d'abord avec le numéro émetteur,
    # puis fallback sur le numéro extrait du body.
    client_row = pg.get_client_by_phone(from_phone)
    if client_row is None and body_phone and body_phone != from_phone:
        client_row = pg.get_client_by_phone(body_phone)

    valid_client = validate_client(client_row)
    if not valid_client.ok:
        logger.info("validation client KO: %s (phone=%s)", valid_client.reason, from_phone)
        return {"status": "skipped", "reason": valid_client.reason}

    # Lookup solde dû côté CRM avec retry sur erreurs transitoires.
    # On skip uniquement si toutes les tentatives ont échoué. Si balance <= 0
    # on continue : le paiement sera enregistré comme avoir/crédit côté UCRM.
    crm_balance = await _get_balance_with_retry(client_row["idclient"])

    valid_balance = validate_crm_balance(crm_balance)
    if not valid_balance.ok:
        logger.info("UCRM injoignable (client=%d) — skip", client_row["idclient"])
        return {"status": "skipped", "reason": valid_balance.reason}

    # Décision métier : faut-il débloquer le client ?
    # - Si payé ≥ dû − tolérance (150 par défaut) ET client suspendu → on débloque.
    # - Sinon : on enregistre le paiement mais on ne touche pas MikroTik/statu
    #   (sous-paiement, ou client déjà actif qui paie en avance).
    amount_paid = int(extracted["montant"])
    was_suspended = client_row.get("statu") == 2
    unblock = was_suspended and should_unblock_client(
        amount_paid=amount_paid,
        crm_balance=crm_balance,
        threshold=config.UNDERPAYMENT_TOLERANCE,
    )
    logger.info(
        "décision : statu=%s balance=%s payé=%s écart=%s → unblock=%s",
        client_row.get("statu"), crm_balance, amount_paid,
        crm_balance - amount_paid, unblock,
    )

    # Le worker débloquera en interrogeant MikroTik par IP au moment du
    # déblocage. On embarque donc l'IP dans le Job ; pas de lookup webhook
    # côté MikroTik. On garde le phone d'origine (from_phone / body_phone)
    # pour le payload — le schéma prod ne stocke plus le téléphone sur le
    # client (info est un texte libre).
    client_phone = body_phone if (body_phone and not from_phone) else from_phone

    # Construction du Job complet — le worker n'aura à faire AUCUN lookup.
    job = Job(
        job_id=queue_store.new_job_id(),
        client=Client(
            id=client_row["idclient"],
            phone=client_phone,
            mac_address=client_row.get("mac"),
            ip_address=client_row.get("ipaddress"),
            current_status="suspended" if was_suspended else "active",
        ),
        payment=Payment(
            amount_mru=amount_paid,
            txn_id=txn_id or "",
            date_heure=extracted.get("date_heure"),
            operator=template,
            crm_balance_before=crm_balance,
            should_unblock=unblock,
        ),
        source=Source(
            wnum=from_phone,
            sample_id=sample_id,
            received_at=_utc_now_iso(),
        ),
    )

    internal_id = queue_store.enqueue(job)
    logger.info(
        "job enqueued id=%d job_id=%s client=%d amount=%d txn=%s",
        internal_id, job.job_id, job.client.id, job.payment.amount_mru, job.payment.txn_id,
    )
    return {
        "status": "enqueued",
        "job_id": job.job_id,
        "client_id": job.client.id,
        "amount_paid": job.payment.amount_mru,
        "crm_balance": crm_balance,
        "should_unblock": unblock,
    }
