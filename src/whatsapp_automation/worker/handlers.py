"""Logique métier du worker : reçoit un Job complet et exécute la séquence.

Aucune lookup DB pour récupérer client/MAC : tout est dans le Job.
Aucune validation métier : déjà faite par le webhook avant l'enqueue.

Ordre des étapes (chacune idempotente, repérée par step_done) :
  1. ucrm.create_payment    → paymentId
  2. db.insert_paiement     (DB locale)
  3. mikrotik.remove_rule   (déblocage)
  4. db.update_client_status → actif (statu=0)
  5. ultramsg.send_document → PDF reçu envoyé au client

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
) -> StepResult:
    """Exécute le job. ``last_step_done`` indique la dernière étape déjà
    faite avec succès lors d'un précédent essai (None si premier essai)."""

    result = StepResult()
    result.ucrm_payment_id = known_payment_id

    if not _should_skip(STEP_PAID_UCRM, last_step_done):
        result.ucrm_payment_id = await ucrm.create_payment(
            client_id=job.client.id,
            amount=job.payment.amount_mru,
            note=f"WhatsApp {job.payment.operator} txn={job.payment.txn_id}",
        )
        logger.info("UCRM payment created: client=%d amount=%d paymentId=%s",
                    job.client.id, job.payment.amount_mru, result.ucrm_payment_id)
        on_step_done(STEP_PAID_UCRM)
        result.completed_steps.append(STEP_PAID_UCRM)

    if not _should_skip(STEP_INSERTED_DB, last_step_done):
        pg.insert_paiement(
            idclient=job.client.id,
            montant=job.payment.amount_mru,
            num=job.client.phone,
            ucrm_payment_id=result.ucrm_payment_id,
            txn_id=job.payment.txn_id,
            operator=job.payment.operator,
        )
        logger.info("DB insert: client=%d amount=%d", job.client.id, job.payment.amount_mru)
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
            rule_id = job.client.firewall_rule_id
            if rule_id:
                removed = await mikrotik.remove_rule(rule_id)
                logger.info("MikroTik unblock: client=%d mac=%s rule=%s ok=%s",
                            job.client.id, job.client.mac_address, rule_id, removed)
            else:
                logger.warning("pas de firewall_rule_id dans le job (client=%d mac=%s) "
                               "— skip unblock", job.client.id, job.client.mac_address)
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
        pdf_url = config.PDF_URL_TEMPLATE.format(payment_id=result.ucrm_payment_id or "0")
        if job.payment.should_unblock:
            caption = "Votre paiement a été reçu. Merci !"
        else:
            owed = job.payment.crm_balance_before - job.payment.amount_mru
            caption = (
                f"Votre paiement de {job.payment.amount_mru} MRU a été enregistré, "
                f"mais il reste {owed} MRU à payer pour réactiver votre connexion."
            )
        await ultramsg.send_document(
            to=f"+222{job.client.phone}",
            document_url=pdf_url,
            filename=f"recu_{result.ucrm_payment_id or 'ND'}.pdf",
            caption=caption,
        )
        logger.info("PDF envoyé via UltraMsg → +222%s (unblocked=%s)",
                    job.client.phone, job.payment.should_unblock)
        on_step_done(STEP_PDF_SENT)
        result.completed_steps.append(STEP_PDF_SENT)

    return result
