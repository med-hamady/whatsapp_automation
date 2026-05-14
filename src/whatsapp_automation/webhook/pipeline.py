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

import logging
from datetime import datetime, timezone
from typing import Optional

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
)


logger = logging.getLogger("whatsapp_automation.webhook.pipeline")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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

    valid_ext = validate_extraction(extracted)
    if not valid_ext.ok:
        logger.info("extraction invalide: %s", valid_ext.reason)
        return {"status": "skipped", "reason": valid_ext.reason}

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

    # Lookup solde dû côté CRM. Si <= 0 → client à jour, on n'empile pas
    # (équivalent du check $amountCRM <= 0 dans remove_suspende_whatsapp.php).
    # En cas d'erreur réseau on continue (le worker fera retry sur POST).
    crm_balance: Optional[int] = None
    try:
        crm_balance = await ucrm.get_balance(client_row["idclient"])
    except Exception as exc:
        logger.warning("UCRM get_balance échoué pour client=%d : %s",
                       client_row["idclient"], exc)

    valid_balance = validate_crm_balance(crm_balance)
    if not valid_balance.ok:
        logger.info("solde CRM <= 0 (client=%d balance=%s) — skip",
                    client_row["idclient"], crm_balance)
        return {"status": "skipped", "reason": valid_balance.reason}

    # Décision métier : faut-il débloquer le client ?
    # - Si payé ≥ dû − tolérance (150 par défaut) → on débloque.
    # - Sinon (sous-paiement > 150) → on enregistre le paiement mais on
    #   garde le client suspendu.
    amount_paid = int(extracted["montant"])
    unblock = should_unblock_client(
        amount_paid=amount_paid,
        crm_balance=crm_balance,
        threshold=config.UNDERPAYMENT_TOLERANCE,
    )
    logger.info(
        "décision : balance=%s payé=%s écart=%s → unblock=%s",
        crm_balance, amount_paid, crm_balance - amount_paid, unblock,
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
            mac_address=client_row["mac"],
            ip_address=client_row.get("ipaddress"),
            current_status="suspended",
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
