"""Construction du Job, partagée entre le webhook et la confirmation dashboard.

Extrait de `pipeline.py` (Phase 4B-1) : la logique de lookup UCRM (avec retry),
de répartition paiement/abonnements suspendus (quels MAC débloquer) et
d'assemblage du `Job` final était dupliquée en un seul bloc dans le pipeline
webhook. Elle est isolée ici pour être réutilisée telle quelle par la future
confirmation dashboard (Phase 4B-2), sans dupliquer ni simplifier les règles
existantes (multi-abonnements, repli MAC local, repli mono-abo historique).

Le pipeline webhook (flux normal UltraMsg) et la confirmation dashboard
partagent : `ucrm_with_retry`, `fetch_ucrm_context`, `compute_unblock_plan`,
`build_job`. Chacun garde son propre enchaînement de validations et son propre
appel à `queue_store.enqueue` — ce module ne fait ni validation de haut niveau
ni enqueue.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

from ..jobqueue import store as queue_store
from ..models import Client, Job, Payment, Source
from ..worker import ucrm
from .validators import plan_unblocks, should_unblock_client


# Code statut "Suspended" d'un service côté UCRM (cf. ucrm._UCRM_SERVICE_STATUS).
UCRM_SERVICE_STATUS_SUSPENDED = 3

logger = logging.getLogger("whatsapp_automation.webhook.job_builder")

# Backoff entre tentatives UCRM get_balance. 3 essais au total ; ~4s max
# d'attente cumulée avant d'abandonner. Appelé depuis une asyncio Task
# détachée (webhook) ou une requête dashboard courte (confirmation) : dans
# les deux cas on ne bloque pas de réponse HTTP synchrone critique.
UCRM_GET_BALANCE_DELAYS = (0, 1, 3)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


async def ucrm_with_retry(factory, label: str, client_id: int):
    """Appelle une coroutine UCRM avec retry sur erreurs transitoires.

    ``factory`` est une fonction sans argument renvoyant la coroutine à
    chaque tentative (ex : ``lambda: ucrm.get_client_details(id)``).

    Retourne le résultat, ou None si toutes les tentatives échouent (timeout,
    réseau, 5xx) OU si UCRM renvoie une 4xx (erreur métier, inutile de retry).
    """
    last_exc: Optional[Exception] = None
    for attempt, delay in enumerate(UCRM_GET_BALANCE_DELAYS, 1):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await factory()
        except httpx.HTTPStatusError as exc:
            # 4xx → erreur métier (client introuvable, token invalide…)
            # inutile de retry, on coupe court.
            if 400 <= exc.response.status_code < 500:
                logger.warning(
                    "UCRM %s client=%d HTTP %d : abandon (pas de retry)",
                    label, client_id, exc.response.status_code,
                )
                return None
            last_exc = exc
        except Exception as exc:
            last_exc = exc
        logger.warning(
            "UCRM %s tentative %d/%d KO client=%d : %s: %r",
            label, attempt, len(UCRM_GET_BALANCE_DELAYS), client_id,
            type(last_exc).__name__, last_exc,
        )
    return None


async def fetch_ucrm_context(idclient: int) -> tuple[Optional[dict], Optional[list[dict]]]:
    """Récupère en parallèle les détails compte et les services UCRM d'un client.

    - ``details`` : solde dû (`accountOutstanding`) + crédit existant. None si
      injoignable après retry (le solde est obligatoire, cf. validate_crm_balance).
    - ``services`` : forfaits (prix + MAC + statut). Peut être None (injoignable) :
      dans ce cas le pipeline retombe sur le mode mono-abo (voir compute_unblock_plan).
    """
    return await asyncio.gather(
        ucrm_with_retry(lambda: ucrm.get_client_details(idclient), "get_client_details", idclient),
        ucrm_with_retry(lambda: ucrm.get_client_services(idclient), "get_client_services", idclient),
    )


def credit_from_details(details: Optional[dict]) -> int:
    """Extrait le crédit disponible (entier MRU, ≥ 0) d'un payload UCRM details."""
    if not details:
        return 0
    try:
        credit = int(round(float(details.get("account_credit") or 0)))
    except (TypeError, ValueError):
        return 0
    return max(0, credit)


@dataclass
class UnblockDecision:
    """Résultat de la décision de déblocage pour un paiement donné.

    - ``unblock_macs`` : MAC des abonnements à débloquer.
    - ``unblock``       : True si au moins un MAC est à débloquer (== bool(unblock_macs)).
    """
    unblock_macs: list[str]
    unblock: bool


def compute_unblock_plan(
    *,
    client_row: dict,
    client_rows: list[dict],
    details: Optional[dict],
    services: Optional[list[dict]],
    amount_paid: int,
    crm_balance: int,
    threshold: int,
) -> UnblockDecision:
    """Décide quels abonnements débloquer pour ce paiement.

    Réplique exactement la règle historique du pipeline webhook :
    - Le client peut payer plusieurs abonnements (services UCRM) en un seul
      versement. On répartit `montant payé + crédit existant` sur les abos
      SUSPENDUS (status UCRM = 3), triés par prix croissant (cf.
      validators.plan_unblocks), et on débloque chaque abo couvert.
    - Filet : des abos suspendus existent mais plan_unblocks n'a rien pu
      débloquer (typiquement parce que les services UCRM n'ont pas de MAC/prix
      exploitable) → si le solde CRM agrégé est couvert par le paiement, repli
      sur les MAC LOCAUX du client (source fiable pour MikroTik).
    - Si aucun service suspendu exploitable (services UCRM indispo, ou client
      déjà actif qui paie en avance) → repli sur la règle mono-abo historique
      basée sur le solde agrégé et le statut local.
    """
    existing_credit = credit_from_details(details)
    available = amount_paid + existing_credit

    # MAC locaux (casse exacte de la DB) indexés pour rattacher chaque service
    # UCRM à sa ligne locale — c'est la casse locale qu'on utilise en aval
    # (update_client_status_by_mac filtre sur `mac` exact).
    local_by_mac = {
        (r.get("mac") or "").strip().lower(): r
        for r in client_rows
        if (r.get("mac") or "").strip()
    }

    suspended_services: list[dict] = []
    for svc in services or []:
        if svc.get("status") != UCRM_SERVICE_STATUS_SUSPENDED:
            continue
        svc_mac = (svc.get("mac") or "").strip()
        local = local_by_mac.get(svc_mac.lower())
        mac = local["mac"] if local else svc_mac
        suspended_services.append({"mac": mac, "price": svc.get("price")})

    if suspended_services:
        plan = plan_unblocks(suspended_services, available, threshold)
        unblock_macs = plan.macs
        logger.info(
            "répartition : abos_suspendus=%d dû_total=%d payé=%d crédit=%d "
            "dispo=%d → débloqués=%d reliquat=%d macs=%s",
            len(suspended_services), plan.total_due, amount_paid, existing_credit,
            available, plan.covered_count, plan.remainder, unblock_macs,
        )
        # Filet : des abos suspendus existent mais plan_unblocks n'a rien pu
        # débloquer — typiquement parce que les services UCRM n'ont pas de
        # `macAddress` (ou pas de prix) exploitable, donc rien à répartir. Si
        # le solde CRM agrégé est couvert par le paiement, on se rabat sur les
        # MAC LOCAUX du client (source fiable, celle qu'utilise le worker pour
        # débloquer via MikroTik). Sans ce filet, un paiement complet reste
        # faussement classé "sous-paiement".
        if not unblock_macs and should_unblock_client(
            amount_paid=amount_paid,
            crm_balance=crm_balance,
            threshold=threshold,
        ):
            unblock_macs = sorted({
                (r.get("mac") or "").strip()
                for r in client_rows
                if (r.get("mac") or "").strip()
                and not (r.get("mac") or "").strip().lower().startswith("pending-")
            })
            logger.info(
                "abos suspendus sans MAC/prix UCRM exploitable mais solde couvert "
                "(balance=%d payé=%d) → repli sur MAC locaux : %s",
                crm_balance, amount_paid, unblock_macs,
            )
    else:
        # Aucun service suspendu exploitable (services UCRM indispo, ou client
        # déjà actif qui paie en avance) → repli sur la règle mono-abo
        # historique basée sur le solde agrégé et le statut local.
        was_suspended = client_row.get("statu") == 2
        primary_mac = (client_row.get("mac") or "").strip()
        fallback_unblock = (
            was_suspended
            and bool(primary_mac)
            and should_unblock_client(
                amount_paid=amount_paid,
                crm_balance=crm_balance,
                threshold=threshold,
            )
        )
        unblock_macs = [primary_mac] if fallback_unblock else []
        logger.info(
            "répartition (repli mono-abo) : statu=%s balance=%s payé=%s "
            "services=%s → unblock=%s",
            client_row.get("statu"), crm_balance, amount_paid,
            "absent" if not services else "vide", bool(unblock_macs),
        )

    return UnblockDecision(unblock_macs=unblock_macs, unblock=bool(unblock_macs))


def build_job(
    *,
    client_row: dict,
    amount_paid: int,
    txn_id: Optional[str],
    date_heure: Optional[str],
    template: str,
    crm_balance: int,
    unblock_macs: list[str],
    phone_for_worker: str,
    wnum: str,
    sample_id: str,
) -> Job:
    """Assemble le Job complet — le worker n'aura à faire AUCUN lookup.

    `phone_for_worker` (Job.client.phone) et `wnum` (Job.source.wnum) sont
    fournis séparément par l'appelant : le flux webhook normal les dérive
    tous deux de from_phone/body_phone (et ils peuvent différer, cf.
    pipeline.process) ; la confirmation dashboard (Phase 4B-2) fixe les deux
    à `original_phone`.
    """
    was_suspended = client_row.get("statu") == 2
    return Job(
        job_id=queue_store.new_job_id(),
        client=Client(
            id=client_row["idclient"],
            phone=phone_for_worker,
            mac_address=client_row.get("mac"),
            ip_address=client_row.get("ipaddress"),
            current_status="suspended" if was_suspended else "active",
        ),
        payment=Payment(
            amount_mru=amount_paid,
            txn_id=txn_id or "",
            date_heure=date_heure,
            operator=template,
            crm_balance_before=crm_balance,
            should_unblock=bool(unblock_macs),
        ),
        source=Source(
            wnum=wnum,
            sample_id=sample_id,
            received_at=utc_now_iso(),
        ),
        unblock_macs=unblock_macs,
    )
