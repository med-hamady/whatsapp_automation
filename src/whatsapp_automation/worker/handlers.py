"""Logique métier du worker : reçoit un Job complet et exécute la séquence.

Aucune lookup DB pour récupérer client/MAC : tout est dans le Job.
Aucune validation métier : déjà faite par le webhook avant l'enqueue.

Ordre des étapes (chacune idempotente, repérée par step_done) :
  1. ucrm.create_payment    → paymentId
  2. db.insert_paiement     (DB locale)
  3. mikrotik.remove_rule   (déblocage)
  4. db.update_client_status → actif (statu=0)
  5. ultramsg.send_document → PDF reçu envoyé au client (caption = détail montants)

Si une étape a déjà été marquée 'step_done', on saute jusqu'à la suivante.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .. import config
from ..db import postgres as pg
from ..models import Job
from . import mikrotik, ucrm, ultramsg


logger = logging.getLogger("whatsapp_automation.worker.handlers")


STEP_PAID_UCRM = "paid_ucrm"
STEP_INSERTED_DB = "inserted_db"
STEP_UNBLOCKED = "unblocked"
STEP_STATUS_ACTIVE = "status_active"
STEP_PDF_SENT = "pdf_sent"

STEPS_ORDER = [
    STEP_PAID_UCRM,
    STEP_INSERTED_DB,
    STEP_UNBLOCKED,
    STEP_STATUS_ACTIVE,
    STEP_PDF_SENT,
]


class StepResult:
    def __init__(self):
        self.ucrm_payment_id: Optional[str] = None
        self.completed_steps: list[str] = []


def _build_message_body(job: Job) -> str:
    """Construit le message WhatsApp envoyé au client après traitement.

    3 montants sont toujours affichés (total dû / payé / reste). Le ton diffère
    selon que le client est débloqué ou pas. Le sur-paiement ajoute une ligne
    `Avoir` pour le crédit en surplus.
    """
    total = job.payment.crm_balance_before
    paid = job.payment.amount_mru
    diff = total - paid                # >0 reste, <0 sur-paiement
    remaining = max(0, diff)
    credit = max(0, -diff)

    lines = [
        f"Montant total : {total} MRU",
        f"Montant payé  : {paid} MRU",
        f"Reste à payer : {remaining} MRU",
    ]
    if credit > 0:
        lines.append(f"Avoir         : {credit} MRU")

    if job.payment.should_unblock:
        header = "✅ Paiement reçu, votre connexion est réactivée."
        footer = "Merci !"
    elif job.client.current_status == "active":
        header = "✅ Paiement reçu, merci."
        footer = "Votre compte est à jour."
    else:
        header = "⚠ Paiement enregistré mais incomplet."
        footer = "Merci de compléter pour réactiver votre connexion."

    return header + "\n\n" + "\n".join(lines) + "\n\n" + footer


def _should_skip(target_step: str, last_done: Optional[str]) -> bool:
    """True si target_step ≤ last_done dans l'ordre des étapes."""
    if last_done is None:
        return False
    try:
        return STEPS_ORDER.index(target_step) <= STEPS_ORDER.index(last_done)
    except ValueError:
        return False


async def process_job(
    job: Job,
    last_step_done: Optional[str],
    on_step_done: Callable[[str], None],
    known_payment_id: Optional[str] = None,
    on_payment_created: Optional[Callable[[str], None]] = None,
) -> StepResult:
    """Exécute le job. ``last_step_done`` indique la dernière étape déjà
    faite avec succès lors d'un précédent essai (None si premier essai).

    ``on_payment_created`` est invoqué dès qu'UCRM a renvoyé le paymentId,
    AVANT de marquer l'étape comme done. C'est ce qui garantit qu'un crash
    juste après l'appel UCRM ne perd pas l'identifiant : il est persisté
    dans la queue avant la moindre étape suivante."""

    result = StepResult()
    result.ucrm_payment_id = known_payment_id

    if not _should_skip(STEP_PAID_UCRM, last_step_done):
        # Note alignée sur la prod PHP (UcrmApiAccess_pay) : "Whatsapp" en dur.
        # L'opérateur et le txn_id restent tracés via les logs / la DB locale.
        result.ucrm_payment_id = await ucrm.create_payment(
            client_id=job.client.id,
            amount=job.payment.amount_mru,
            note="Whatsapp",
        )
        logger.info(
            "UCRM payment created: client=%d amount=%d paymentId=%s operator=%s txn=%s",
            job.client.id, job.payment.amount_mru, result.ucrm_payment_id,
            job.payment.operator, job.payment.txn_id,
        )
        if on_payment_created is not None and result.ucrm_payment_id:
            on_payment_created(result.ucrm_payment_id)
        on_step_done(STEP_PAID_UCRM)
        result.completed_steps.append(STEP_PAID_UCRM)

    if not _should_skip(STEP_INSERTED_DB, last_step_done):
        if not result.ucrm_payment_id:
            # Ne devrait pas arriver : on n'atteint cette étape qu'après UCRM.
            raise RuntimeError(
                f"insert_paiement sans paymentId UCRM (job={job.job_id})"
            )
        pg.insert_paiement(
            idclient=job.client.id,
            amount=job.payment.amount_mru,
            phone=job.client.phone,
            id_payment=result.ucrm_payment_id,
            txn_id=job.payment.txn_id,
        )
        logger.info(
            "DB insert: client=%d amount=%d id_payment=%s",
            job.client.id, job.payment.amount_mru, result.ucrm_payment_id,
        )
        on_step_done(STEP_INSERTED_DB)
        result.completed_steps.append(STEP_INSERTED_DB)

    if not _should_skip(STEP_UNBLOCKED, last_step_done):
        if not job.payment.should_unblock:
            logger.info(
                "sous-paiement (balance=%d payé=%d écart=%d) — paiement enregistré "
                "mais client NON débloqué (client=%d)",
                job.payment.crm_balance_before, job.payment.amount_mru,
                job.payment.crm_balance_before - job.payment.amount_mru,
                job.client.id,
            )
        else:
            mac = job.client.mac_address
            # Le préfixe `pending-` est un placeholder posé par le sync UCRM
            # pour les clients pas encore provisionnés côté MikroTik
            # (cf. scripts/sync_clients_from_ucrm.py). Pas de règle à supprimer.
            if mac and not mac.startswith("pending-"):
                removed = await mikrotik.unblock_by_mac(mac)
                logger.info("MikroTik unblock: client=%d mac=%s rules_removed=%d",
                            job.client.id, mac, removed)
            else:
                logger.warning("pas de mac_address valide dans le job (client=%d "
                               "mac=%r ip=%s) — skip unblock",
                               job.client.id, mac, job.client.ip_address)
        on_step_done(STEP_UNBLOCKED)
        result.completed_steps.append(STEP_UNBLOCKED)

    if not _should_skip(STEP_STATUS_ACTIVE, last_step_done):
        if job.payment.should_unblock:
            pg.update_client_status(job.client.id, statu=0)
            logger.info("Statut client %d → actif", job.client.id)
        else:
            logger.info("Statut client %d inchangé (sous-paiement)", job.client.id)
        on_step_done(STEP_STATUS_ACTIVE)
        result.completed_steps.append(STEP_STATUS_ACTIVE)

    if not _should_skip(STEP_PDF_SENT, last_step_done):
        caption = _build_message_body(job)
        pdf_url = config.PDF_URL_TEMPLATE.format(
            payment_id=result.ucrm_payment_id or "0",
        )
        await ultramsg.send_document(
            to=f"+222{job.client.phone}",
            document_url=pdf_url,
            filename=f"recu_{result.ucrm_payment_id or 'ND'}.pdf",
            caption=caption,
        )
        logger.info("PDF envoyé via UltraMsg → +222%s (unblocked=%s) url=%s",
                    job.client.phone, job.payment.should_unblock, pdf_url)
        on_step_done(STEP_PDF_SENT)
        result.completed_steps.append(STEP_PDF_SENT)

    return result
