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
from typing import Optional

from .. import config
from ..jobqueue import store as queue_store
from ..db import postgres as pg
from ..worker import ultramsg
from . import job_builder
from .ai_ocr_client import extract as ai_ocr_extract
from .dashboard import unknown_clients_store
from .image_downloader import download as download_image
from .phone import parse_body_number, parse_from_field
from .validators import (
    validate_client,
    validate_crm_balance,
    validate_document_type,
    validate_extraction,
    validate_no_transaction_error,
    validate_payment_balance,
    validate_recipient_name,
)


logger = logging.getLogger("whatsapp_automation.webhook.pipeline")


# Libellés humains pour les raisons d'échec qu'on notifie au support. Les
# clefs correspondent exactement aux `reason` retournés par les validateurs.
# Les autres types d'échec (OCR raté, image illisible, sur-paiement…) ne
# sont PAS notifiés : le client peut simplement renvoyer une meilleure photo.
_FAILURE_LABELS: dict[str, str] = {
    "client_not_found": "Client introuvable dans le CRM",
    "crm_unreachable": "CRM injoignable",
    "transaction_error": "Transaction ÉCHOUÉE (capture d'erreur) — non encaissée",
}


def _format_source(from_phone: str, group_id: Optional[str]) -> str:
    """Compose le libellé source pour la notif support :
    - `+22237697850`                      (privé)
    - `+22237697850 (groupe 120363…)`     (groupe + expéditeur connu)
    - `groupe 120363…`                    (groupe + expéditeur inconnu)
    - `inconnu`                           (rien d'identifiable)
    """
    client_num = f"+222{from_phone}" if from_phone else ""
    if group_id and client_num:
        return f"{client_num} (groupe {group_id})"
    if group_id:
        return f"groupe {group_id}"
    return client_num or "inconnu"


async def _notify_support_failure(
    *,
    reason: str,
    from_phone: str,
    media_url: Optional[str],
    group_id: Optional[str],
) -> None:
    """Notifie le destinataire support quand un paiement n'a pas pu être traité.

    - Si `media_url` est connu : envoie l'image avec un caption (libellé + source).
    - Sinon : envoie un message texte seul (ex : image_download_failed).

    Best-effort : toute erreur (UltraMsg KO, SUPPORT_RECIPIENT vide…) est
    logguée mais ne propage pas — on ne veut pas casser le retour webhook.
    """
    if not config.SUPPORT_RECIPIENT:
        logger.warning(
            "paiement échec reason=%s mais SUPPORT_RECIPIENT non configuré",
            reason,
        )
        return

    label = _FAILURE_LABELS.get(reason, f"Paiement échec ({reason})")
    source = _format_source(from_phone, group_id)
    body = f"{label} : {source}"

    try:
        if media_url:
            await ultramsg.send_image(
                to=config.SUPPORT_RECIPIENT,
                image_url=media_url,
                caption=body,
            )
        else:
            await ultramsg.send_chat(
                to=config.SUPPORT_RECIPIENT,
                body=body,
            )
        logger.info(
            "support notifié reason=%s from=%s group=%s media=%s",
            reason, from_phone or "-", group_id or "-",
            "yes" if media_url else "no",
        )
    except Exception as exc:
        logger.warning(
            "échec notification support reason=%s from=%s group=%s : %s: %r",
            reason, from_phone or "-", group_id or "-",
            type(exc).__name__, exc,
        )


def _save_unknown_client(
    *,
    sample_id: str,
    txn_id: Optional[str],
    amount: Optional[int],
    date_heure: Optional[str],
    operator: str,
    from_phone: str,
    body_phone: Optional[str],
    group_id: Optional[str],
    raw_text: str,
) -> None:
    """Préserve les données du paiement dans `numeros_introuvable` (aucun Job
    créé, aucun appel UCRM/MikroTik). insert_unknown_client() est déjà
    best-effort (erreur SQLite loggée, jamais levée)."""
    unknown_clients_store.insert_unknown_client(
        sample_id=sample_id,
        txn_id=txn_id,
        amount=amount,
        date_heure=date_heure,
        operator=operator,
        whatsapp_phone=from_phone,
        body_phone=body_phone,
        group_id=group_id,
        raw_text=raw_text,
    )


async def process(payload: dict) -> dict:
    """Traite un payload UltraMsg. Retourne un résumé (pour logs/debug ;
    UltraMsg n'utilise pas la réponse au-delà du status 200)."""

    event_data = (payload.get("data")
                  or payload.get("event", {}).get("data")
                  or {})
    raw_from = event_data.get("from", "") or ""
    raw_author = event_data.get("author", "") or ""
    # Messages de groupe WhatsApp : `from` se termine par `@g.us` (ex:
    # 120363xxxxxxxxxxxx@g.us). Dans ce cas, le vrai expéditeur est dans
    # `author` (format <numéro>@c.us). On garde l'ID du groupe pour le
    # signaler au support, et on utilise `author` comme téléphone client.
    is_group = raw_from.endswith("@g.us")
    group_id = raw_from.split("@")[0] if is_group else None
    sender_field = raw_author if is_group else raw_from

    from_phone = parse_from_field(sender_field)
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

    # Écarte les documents non-paiement (ex : fiche "NOUVEL ABONNEMENT" de
    # Connect A2 qu'un client envoie parfois par erreur). Pas de notif support :
    # ce n'est pas un paiement raté, juste un document hors-sujet.
    valid_doc = validate_document_type(raw_text, template)
    if not valid_doc.ok:
        logger.info("document non-paiement écarté: %s (from=%s)", valid_doc.reason, from_phone)
        return {"status": "skipped", "reason": valid_doc.reason}

    # Transaction échouée : la capture montre une pop-up « Erreur » (le paiement
    # n'est pas passé côté opérateur). Le reçu sous-jacent reste lisible donc le
    # montant/bénéficiaire seraient extraits à tort → on rejette et on notifie le
    # support (le client a tenté un paiement qui a échoué).
    valid_txn = validate_no_transaction_error(raw_text)
    if not valid_txn.ok:
        logger.info(
            "transaction échouée (capture d'erreur) écartée (from=%s group=%s)",
            from_phone, group_id or "-",
        )
        await _notify_support_failure(
            reason="transaction_error",
            from_phone=from_phone,
            media_url=media_url,
            group_id=group_id,
        )
        return {"status": "skipped", "reason": valid_txn.reason}

    valid_ext = validate_extraction(extracted)
    if not valid_ext.ok:
        logger.info("extraction invalide: %s", valid_ext.reason)
        return {"status": "skipped", "reason": valid_ext.reason}

    # Détection (mode observation) : on vérifie que PATRINET / A2 CONNECT /
    # PATRIE NET apparaît dans la capture, mais on NE BLOQUE PAS le paiement
    # en cas d'échec. On loggue un WARNING pour pouvoir mesurer le taux de
    # mismatch en prod avant de durcir en rejet effectif.
    valid_recipient = validate_recipient_name(template, raw_text)
    if not valid_recipient.ok:
        logger.warning(
            "destinataire suspect (PASS-THROUGH) : %s (template=%s, from=%s)",
            valid_recipient.reason, template, from_phone,
        )

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

    # Lookup client (DB locale) — d'abord avec le numéro émetteur, puis
    # fallback sur le numéro extrait du body. Un client peut avoir PLUSIEURS
    # abonnements (autant de lignes que de MAC, même idclient) : on récupère
    # toutes les lignes pour pouvoir débloquer chaque abonnement payé.
    client_rows = pg.get_clients_by_phone(from_phone)
    if not client_rows and body_phone and body_phone != from_phone:
        client_rows = pg.get_clients_by_phone(body_phone)

    # Repli : un ticket `numeros_introuvable` précédent de ce numéro a déjà
    # été confirmé (queued) suite à un rattachement manuel dans le dashboard —
    # cf. unknown_clients_store.find_client_id_for_phone. Consultée UNIQUEMENT
    # si les deux lookups téléphone ont échoué — elle ne remplace jamais le
    # lookup normal. Best-effort : ticket introuvable ou correspondance
    # périmée (client supprimé de PostgreSQL) → on retombe sur le
    # client_not_found historique.
    if not client_rows and from_phone:
        mapped_client_id = unknown_clients_store.find_client_id_for_phone(from_phone)
        if mapped_client_id:
            try:
                client_rows = pg.get_client_by_id(mapped_client_id)
            except Exception as exc:
                logger.warning(
                    "repli numéro→client : get_client_by_id(%s) KO : %s: %r",
                    mapped_client_id, type(exc).__name__, exc,
                )
                client_rows = []
            if client_rows:
                logger.info(
                    "client %s retrouvé via correspondance numéro→client (phone=%s)",
                    mapped_client_id, from_phone,
                )
            else:
                logger.warning(
                    "correspondance numéro→client périmée : idclient=%s introuvable (phone=%s)",
                    mapped_client_id, from_phone,
                )

    client_row = client_rows[0] if client_rows else None

    valid_client = validate_client(client_row)
    if not valid_client.ok:
        logger.info(
            "validation client KO: %s (phone=%s group=%s)",
            valid_client.reason, from_phone, group_id or "-",
        )
        if valid_client.reason == "client_not_found":
            _save_unknown_client(
                sample_id=sample_id,
                txn_id=txn_id,
                amount=extracted.get("montant"),
                date_heure=extracted.get("date_heure"),
                operator=template,
                from_phone=from_phone,
                body_phone=body_phone,
                group_id=group_id,
                raw_text=raw_text,
            )
            await _notify_support_failure(
                reason="client_not_found",
                from_phone=from_phone,
                media_url=media_url,
                group_id=group_id,
            )
        return {"status": "skipped", "reason": valid_client.reason}

    # Lookup CRM avec retry sur erreurs transitoires, en parallèle :
    #  - détails compte (solde dû `accountOutstanding` + crédit existant)
    #  - services/forfaits (prix + MAC + statut par abonnement)
    # On skip uniquement si les DÉTAILS ont échoué (le solde est obligatoire).
    # Les services peuvent manquer (None) → on retombe sur le mode mono-abo.
    idclient = client_row["idclient"]
    details, services = await job_builder.fetch_ucrm_context(idclient)

    crm_balance = int(details.get("balance") or 0) if details else None

    valid_balance = validate_crm_balance(crm_balance)
    if not valid_balance.ok:
        logger.info("UCRM injoignable (client=%d) — skip", client_row["idclient"])
        await _notify_support_failure(
            reason="crm_unreachable",
            from_phone=from_phone,
            media_url=media_url,
            group_id=group_id,
        )
        return {"status": "skipped", "reason": valid_balance.reason}

    amount_paid = int(extracted["montant"])

    # Anti sur-paiement : si le montant payé dépasse ce qu'il reste à devoir
    # ET que la balance restante est ≤ tolérance (compte quasi à jour), on
    # refuse silencieusement — sans doute capture rejouée ou erreur client.
    # Sinon (sous-paiement, exact, ou sur-paiement sur compte avec dette
    # significative), on laisse passer.
    valid_overpay = validate_payment_balance(
        amount_paid=amount_paid,
        crm_balance=crm_balance,
        threshold=config.UNDERPAYMENT_TOLERANCE,
    )
    if not valid_overpay.ok:
        logger.info(
            "paiement refusé : %s (client=%d balance=%d payé=%d txn=%s)",
            valid_overpay.reason, client_row["idclient"], crm_balance,
            amount_paid, txn_id or "",
        )
        return {"status": "skipped", "reason": valid_overpay.reason}

    # Décision métier : quels abonnements débloquer ? Le client peut payer
    # plusieurs abonnements (services UCRM) en un seul versement — cf.
    # job_builder.compute_unblock_plan pour la règle complète (répartition,
    # repli MAC local, repli mono-abo historique).
    decision = job_builder.compute_unblock_plan(
        client_row=client_row,
        client_rows=client_rows,
        details=details,
        services=services,
        amount_paid=amount_paid,
        crm_balance=crm_balance,
        threshold=config.UNDERPAYMENT_TOLERANCE,
    )
    unblock_macs = decision.unblock_macs
    unblock = decision.unblock

    # Le worker débloquera en interrogeant MikroTik par IP au moment du
    # déblocage. On embarque donc l'IP dans le Job ; pas de lookup webhook
    # côté MikroTik. On garde le phone d'origine (from_phone / body_phone)
    # pour le payload — le schéma prod ne stocke plus le téléphone sur le
    # client (info est un texte libre).
    client_phone = body_phone if (body_phone and not from_phone) else from_phone

    # Construction du Job complet — le worker n'aura à faire AUCUN lookup.
    job = job_builder.build_job(
        client_row=client_row,
        amount_paid=amount_paid,
        txn_id=txn_id,
        date_heure=extracted.get("date_heure"),
        template=template,
        crm_balance=crm_balance,
        unblock_macs=unblock_macs,
        phone_for_worker=client_phone,
        wnum=from_phone,
        sample_id=sample_id,
    )

    internal_id = queue_store.enqueue(job)
    if internal_id is None:
        # Doublon détecté atomiquement dans enqueue() — un autre webhook du
        # même reçu (txn_id) a gagné la course. Les checks is_txn_* en amont
        # ont raté la fenêtre TOCTOU mais l'INSERT atomique a fermé le trou.
        logger.info(
            "idempotence atomique: txn_id %s déjà traité ou en queue (course gagnée ailleurs)",
            job.payment.txn_id,
        )
        return {"status": "skipped", "reason": "duplicate_race"}
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
        "unblock_macs": unblock_macs,
    }
