"""Validations métier appliquées avant la mise en queue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ValidationResult:
    ok: bool
    reason: Optional[str] = None


def validate_extraction(extracted: dict) -> ValidationResult:
    """Vérifie que l'IA a extrait au moins le montant."""
    if not extracted:
        return ValidationResult(False, "no_extraction")
    montant = extracted.get("montant")
    if montant is None or not isinstance(montant, int) or montant <= 0:
        return ValidationResult(False, "no_or_invalid_amount")
    return ValidationResult(True)


def validate_client(client: dict | None) -> ValidationResult:
    if client is None:
        return ValidationResult(False, "client_not_found")
    if client.get("statu") != 1:
        return ValidationResult(False, "client_not_suspended")
    if not client.get("mac"):
        return ValidationResult(False, "client_has_no_mac")
    return ValidationResult(True)


def validate_amount(montant_paye: int, client_balance: Optional[int] = None) -> ValidationResult:
    """Le montant payé doit être > 0 ; si on connait le solde dû, on tolère
    le paiement même s'il diffère (parfois le client paie un peu plus/moins),
    mais on log."""
    if montant_paye <= 0:
        return ValidationResult(False, "invalid_amount")
    return ValidationResult(True)


def validate_crm_balance(balance: Optional[int]) -> ValidationResult:
    """Le solde CRM est OBLIGATOIRE pour décider s'il faut débloquer.
    - None (CRM injoignable) → on n'empile pas, on attend que CRM remonte.
    - <= 0 → client déjà à jour, rien à faire.
    - > 0 → solde valide, on peut continuer.
    """
    if balance is None:
        return ValidationResult(False, "crm_unreachable")
    if balance <= 0:
        return ValidationResult(False, "client_already_paid_up")
    return ValidationResult(True)


def should_unblock_client(amount_paid: int, crm_balance: int, threshold: int) -> bool:
    """Règle métier : on débloque le client si l'écart entre ce qu'il doit
    (CRM) et ce qu'il a payé est ≤ threshold.
    - balance=1500, paid=1500 → écart=0   → unblock ✅
    - balance=1500, paid=1400 → écart=100 → unblock ✅ (sous-paiement toléré)
    - balance=1500, paid=1349 → écart=151 → NO unblock (paiement enregistré)
    - balance=1500, paid=1600 → écart=-100 → unblock ✅ (sur-paiement = avoir)
    """
    underpayment = crm_balance - amount_paid
    return underpayment <= threshold
