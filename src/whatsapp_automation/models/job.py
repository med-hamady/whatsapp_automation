"""Modèles Pydantic : contrat entre webhook et worker.

Le worker reçoit un Job COMPLET et n'a jamais à interroger la DB pour
récupérer des infos client/paiement. Toute la préparation est faite côté
webhook.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Client(BaseModel):
    id: int
    phone: str                            # ex "37697850" sans indicatif
    mac_address: Optional[str] = None     # ex "AA:BB:CC:DD:EE:FF" — None si client jamais provisionné MikroTik
    ip_address: Optional[str] = None      # IP attribuée au client (sert au déblocage MikroTik)
    current_status: str                   # "suspended" | "active"


class Payment(BaseModel):
    amount_mru: int                       # montant extrait de l'image (ce qu'on va créditer)
    txn_id: str
    date_heure: Optional[str] = None      # ISO 8601
    operator: str                         # "bankily" | "sedad" | "masrvi" | "generic"
    crm_balance_before: int               # solde dû côté CRM au moment de l'enqueue
    should_unblock: bool                  # décision calculée par le webhook :
                                          # True si (balance - amount) <= tolérance.
                                          # Si False : on encaisse le paiement
                                          # mais on ne supprime PAS la règle firewall
                                          # et on ne passe PAS le statut à actif.


class Source(BaseModel):
    wnum: str                             # numéro WhatsApp d'envoi
    sample_id: str                        # référence dataset ai_ocr
    received_at: str                      # ISO 8601 UTC


class Job(BaseModel):
    job_id: str = Field(..., description="UUID unique du job")
    client: Client
    payment: Payment
    source: Source

    # MAC des abonnements à débloquer pour ce paiement. Un client peut payer
    # plusieurs abonnements (services UCRM) en un seul versement : on débloque
    # alors chaque abonnement couvert par le montant (cf. validators.plan_unblocks).
    # Défaut vide → les jobs déjà en queue (schéma antérieur) restent valides ;
    # le worker retombe alors sur `client.mac_address` si `should_unblock`.
    unblock_macs: list[str] = Field(default_factory=list)
