"""Test unitaire de validate_document_type (détection fiche d'abonnement).

Vérifie que :
  1. Une fiche "NOUVEL ABONNEMENT" Connect A2 est rejetée (subscription_form).
  2. Un OCR fragmenté/collé de la même fiche est rejeté.
  3. Un vrai reçu de paiement (Bankily/Masrvi) passe.
  4. Un texte vide passe (on laisse les autres validations décider).
  5. Un seul marqueur isolé ne suffit pas (pas de faux positif).

Usage :
    python scripts/test_document_type.py
"""

from __future__ import annotations

import sys

# Console Windows par défaut en cp1252 : on force UTF-8 pour les libellés
# (flèches, arabe) sans avoir à définir PYTHONIOENCODING.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from whatsapp_automation.webhook.validators import validate_document_type
from whatsapp_automation.ai_ocr.extractors import extract as run_extractors


def _check(label: str, cond: bool, detail: str = "") -> None:
    status = "OK " if cond else "FAIL"
    print(f"  [{status}] {label}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        sys.exit(1)


# Texte OCR typique de la fiche Connect A2 (telle qu'envoyée par erreur).
FICHE_ABONNEMENT = """
Connect A2 - always be connected
NOUVEL ABONNEMENT  إشتراك جديد
Identification du client :
Nom ou raison sociale : Mohamed Diop
Adresse : Basra
N WhatsApp : 46714525
N Tel : 48668928
Date demande : 13/06/2026
Nombres d'étages de la propriété :
Forfait / Package        Classique  Home  Pro  Commerce
AirFiber Familial (15Mbps)
AirFiber Livebox (20Mbps)  X
Date d'installation : 13/06/2026
Signature
Service distribution   Technicien   Client
"""

# Même fiche mais OCR qui colle/fragmente (cas réaliste).
FICHE_OCR_FRAGMENTE = "nouvelabonnement forfaitpackage airfiber datedinstallation"

# Vrai reçu Bankily.
RECU_BANKILY = """
Bankily
Transfert reçu
Montant : 1500 MRU
Vers PATRINET NKTT
Reference: 123456789
Date: 13/06/2026 14:32
"""

# Reçu Masrvi.
RECU_MASRVI = """
Masrvi
Paiement marchand
Commerçant: CONNECT
Code: A2
Montant 2000 MRU
TXN 998877
"""


def main() -> None:
    print("Test validate_document_type :")

    r = validate_document_type(FICHE_ABONNEMENT)
    _check("fiche abonnement → rejetée", not r.ok, r.reason or "")
    _check("fiche abonnement → reason=subscription_form", r.reason == "subscription_form")

    r = validate_document_type(FICHE_OCR_FRAGMENTE)
    _check("fiche OCR fragmenté → rejetée", not r.ok, r.reason or "")

    r = validate_document_type(RECU_BANKILY)
    _check("reçu Bankily → accepté", r.ok, r.reason or "")

    r = validate_document_type(RECU_MASRVI)
    _check("reçu Masrvi → accepté", r.ok, r.reason or "")

    r = validate_document_type("")
    _check("texte vide → accepté", r.ok)

    r = validate_document_type(None)
    _check("None → accepté", r.ok)

    # Un seul marqueur isolé ne doit pas suffire (anti faux-positif).
    r = validate_document_type("Paiement abonnement AirFiber 1500 MRU PATRINET")
    _check("un seul marqueur → accepté", r.ok, r.reason or "")

    # Signal template OCR : prioritaire, même sans raw_text.
    r = validate_document_type("", template="subscription_form")
    _check("template=subscription_form → rejeté", not r.ok and r.reason == "subscription_form")
    r = validate_document_type(RECU_BANKILY, template="bankily")
    _check("template=bankily → accepté", r.ok, r.reason or "")

    print("\nTest extracteur OCR (classification template) :")

    # La clause légale "résiliation ou non-paiement" faisait basculer la fiche
    # en 'sedad' : on vérifie qu'elle est désormais classée subscription_form.
    fiche_clause = FICHE_ABONNEMENT + "\nle client porte garant ... après résiliation ou non-paiement."
    res = run_extractors(fiche_clause)
    _check("fiche (clause non-paiement) → template=subscription_form",
           res.template == "subscription_form", res.template)
    _check("fiche → montant non extrait (None)", res.extracted.montant is None,
           str(res.extracted.montant))

    # Un vrai reçu Sedad doit rester 'sedad', pas volé par le nouvel extracteur.
    recu_sedad = "SEDAD BANK PAIEMENT PATRIE NET 01471 1.500,00 TR06124615221"
    res = run_extractors(recu_sedad)
    _check("reçu Sedad → reste template=sedad", res.template == "sedad", res.template)

    print("\nTous les checks OK.")


if __name__ == "__main__":
    main()
