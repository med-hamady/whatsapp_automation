"""Validations métier appliquées avant la mise en queue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ValidationResult:
    ok: bool
    reason: Optional[str] = None


# Noms du destinataire (compte qui reçoit l'argent) attendus dans le texte
# OCR, par template d'opérateur. Match par sous-chaîne après normalisation
# (majuscules, espaces collapsés). Ces libellés sont constants quelle que
# soit la langue de la capture (arabe, français).
RECIPIENT_NAMES_BY_TEMPLATE: dict[str, tuple[str, ...]] = {
    "masrvi": ("A2 CONNECT",),
    "bankily": ("PATRINET NKTT", "PATRINET"),
    "sedad": ("PATRIE NET",),
}

# Identifiants numériques additionnels acceptés en plus du nom. Pour bankily,
# les notifications SMS et les écrans B-PAY n'affichent souvent que le
# numéro Bankily du destinataire (34610101) ou le code marchand B-PAY
# (016456) — sans jamais montrer "PATRINET". Ces deux chaînes sont des
# identifiants constants du compte PATRINET, on les accepte comme preuve.
RECIPIENT_IDS_BY_TEMPLATE: dict[str, tuple[str, ...]] = {
    "bankily": ("34610101", "016456"),
}

# Fallback pour template "generic" : on n'a pas pu typer le reçu, on accepte
# si n'importe lequel des noms attendus apparaît dans le texte.
_ALL_RECIPIENT_NAMES: tuple[str, ...] = tuple(
    name
    for names in RECIPIENT_NAMES_BY_TEMPLATE.values()
    for name in names
)
_ALL_RECIPIENT_IDS: tuple[str, ...] = tuple(
    _id
    for ids in RECIPIENT_IDS_BY_TEMPLATE.values()
    for _id in ids
)


def _compact(text: str) -> str:
    """Uppercase + retire tout ce qui n'est pas lettre/chiffre.

    L'OCR fragmente fréquemment les libellés : 'A2 CONNECT' peut sortir collé
    ('A2CONNECT'), espacé, ou séparé par d'autres mots ('Commerçant: CONNECT
    Code: A2'). Compacter normalise les deux premiers cas.
    """
    return "".join(ch for ch in text.upper() if ch.isalnum())


def _name_in_text(name: str, text_compact: str) -> bool:
    """True si le nom destinataire apparaît dans le texte OCR (compacté).

    Deux stratégies essayées dans l'ordre :
    1. Nom compacté présent comme sous-chaîne contiguë (gère 'A2CONNECT'
       collé et 'PATRIENET' sans espace, courants en sortie OCR).
    2. Pour les noms multi-tokens : tous les tokens présents (chacun >= 2
       caractères) dans le texte compacté. Gère l'écran Masrvi qui affiche
       'Commerçant: CONNECT' et 'Code: A2' sur des lignes séparées avec
       d'autres mots entre les deux.
    """
    expected_compact = _compact(name)
    if expected_compact and expected_compact in text_compact:
        return True
    tokens = [_compact(t) for t in name.upper().split() if len(t) >= 2]
    if len(tokens) >= 2 and all(t in text_compact for t in tokens):
        return True
    return False


# Marqueurs (compactés : majuscules sans espace/ponctuation) propres aux
# fiches "NOUVEL ABONNEMENT" de Connect A2. Ce sont des formulaires
# d'inscription d'un nouveau client (nom, adresse, forfait, date
# d'installation, signature) — PAS des reçus de paiement. Un client les
# envoie parfois par erreur sur le même canal WhatsApp.
#
# On exige au moins DEUX marqueurs distincts pour conclure : un vrai reçu de
# paiement ne contient quasi jamais deux de ces libellés à la fois, alors que
# la fiche en contient une dizaine. Cela évite les faux positifs (un message
# de paiement qui mentionnerait au passage "abonnement").
_SUBSCRIPTION_FORM_MARKERS: tuple[str, ...] = (
    "NOUVELABONNEMENT",       # titre de la fiche
    "FORFAITPACKAGE",         # "Forfait / Package"
    "AIRFIBER",               # noms de forfaits (AirFiber Familial/Pro/Turbo…)
    "DATEDINSTALLATION",      # "Date d'installation"
    "SERVICEDISTRIBUTION",    # case de signature
    "RAISONSOCIALE",          # "Nom ou raison sociale"
    "IDENTIFICATIONDUCLIENT", # en-tête
    "NOMBRESDETAGES",         # "Nombres d'étages de la propriété"
    "إشتراكجديد",             # "Nouvel abonnement" en arabe
)
_SUBSCRIPTION_FORM_MIN_HITS = 2


def validate_document_type(
    raw_text: Optional[str], template: Optional[str] = None
) -> ValidationResult:
    """Écarte les documents qui ne sont pas des reçus de paiement.

    Aujourd'hui un seul type parasite est connu : la fiche "NOUVEL
    ABONNEMENT" de Connect A2.

    Deux signaux, dans l'ordre :
    1. ``template == "subscription_form"`` : la couche OCR a déjà classé le
       document (cf. ai_ocr.extractors.subscription_form). C'est la source de
       vérité quand le service OCR est à jour.
    2. Sinon, repli défensif : on scanne ``raw_text`` et on rejette si au
       moins ``_SUBSCRIPTION_FORM_MIN_HITS`` marqueurs distincts apparaissent
       (utile si l'OCR n'a pas encore été redéployé).

    Retourne ``ok=False, reason="subscription_form"`` si c'est une fiche
    d'abonnement, ``ok=True`` sinon (y compris si ``raw_text`` est vide).
    """
    if template == "subscription_form":
        return ValidationResult(False, "subscription_form")
    if not raw_text:
        return ValidationResult(True)
    text_compact = _compact(raw_text)
    hits = sum(1 for marker in _SUBSCRIPTION_FORM_MARKERS if marker in text_compact)
    if hits >= _SUBSCRIPTION_FORM_MIN_HITS:
        return ValidationResult(False, "subscription_form")
    return ValidationResult(True)


def validate_extraction(extracted: dict) -> ValidationResult:
    """Vérifie que l'IA a extrait au moins le montant."""
    if not extracted:
        return ValidationResult(False, "no_extraction")
    montant = extracted.get("montant")
    if montant is None or not isinstance(montant, int) or montant <= 0:
        return ValidationResult(False, "no_or_invalid_amount")
    return ValidationResult(True)


def validate_recipient_name(template: str, raw_text: Optional[str]) -> ValidationResult:
    """Vérifie que le destinataire attendu apparaît dans le texte OCR.

    Trois preuves possibles, dans l'ordre :
    1. Nom littéral (PATRINET, A2 CONNECT, PATRIE NET) propre au template.
    2. Identifiant numérique connu (ex : numéro Bankily 34610101 ou code
       B-PAY 016456 pour PATRINET), utile pour les notifications SMS qui
       n'affichent jamais le nom.
    3. Pour "generic" (template non identifié) : on tente n'importe quel
       nom ou identifiant des 3 opérateurs.

    Template inattendu (ni l'un des 3 ni 'generic') → rejet par défaut.
    """
    if template in RECIPIENT_NAMES_BY_TEMPLATE:
        expected_names = RECIPIENT_NAMES_BY_TEMPLATE[template]
        expected_ids = RECIPIENT_IDS_BY_TEMPLATE.get(template, ())
    elif template == "generic":
        expected_names = _ALL_RECIPIENT_NAMES
        expected_ids = _ALL_RECIPIENT_IDS
    else:
        return ValidationResult(False, "recipient_unknown_template")

    if not raw_text:
        return ValidationResult(False, "recipient_name_missing")

    text_compact = _compact(raw_text)
    for name in expected_names:
        if _name_in_text(name, text_compact):
            return ValidationResult(True)
    for ident in expected_ids:
        if _compact(ident) in text_compact:
            return ValidationResult(True)
    return ValidationResult(False, "recipient_name_mismatch")


def validate_client(client: dict | None) -> ValidationResult:
    """On accepte tous les statuts : un client actif peut payer en avance, ou
    avoir été débloqué entre-temps par un autre canal. Le pipeline décidera
    s'il faut effectivement débloquer MikroTik via `should_unblock`.

    La MAC reste indispensable côté schéma (NOT NULL en prod) ; on n'exige plus
    qu'elle soit utilisable pour MikroTik puisqu'un client déjà actif n'a pas
    de règle à supprimer.
    """
    if client is None:
        return ValidationResult(False, "client_not_found")
    return ValidationResult(True)


def validate_amount(montant_paye: int, client_balance: Optional[int] = None) -> ValidationResult:
    """Le montant payé doit être > 0 ; si on connait le solde dû, on tolère
    le paiement même s'il diffère (parfois le client paie un peu plus/moins),
    mais on log."""
    if montant_paye <= 0:
        return ValidationResult(False, "invalid_amount")
    return ValidationResult(True)


def validate_crm_balance(balance: Optional[int]) -> ValidationResult:
    """Le solde CRM est OBLIGATOIRE pour pouvoir construire le job (sert au
    message client et à la décision de déblocage).
    - None (CRM injoignable) → on n'empile pas, on attend que CRM remonte.
    - <= 0 → client déjà à jour ou en avoir : on enregistre quand même le
      paiement (UCRM créera un crédit/avoir), on ne débloque juste pas.
    - > 0 → solde dû, traitement normal.
    """
    if balance is None:
        return ValidationResult(False, "crm_unreachable")
    return ValidationResult(True)


def validate_payment_balance(
    amount_paid: int, crm_balance: int, threshold: int
) -> ValidationResult:
    """Anti sur-paiement : refuse les paiements quand il ne reste presque
    rien à devoir.

    - balance ≥ montant payé → OK (paiement normal, exact ou sous-paiement)
    - balance < montant payé (sur-paiement) :
      - balance ≤ threshold → refus (rien d'utile à régler, sans doute capture
        rejouée ou erreur côté client)
      - balance > threshold → OK (sur-paiement toléré, créera un avoir)

    Le seuil est partagé avec `should_unblock_client` (UNDERPAYMENT_TOLERANCE) :
    en dessous de cette valeur, on considère le compte déjà à jour.
    """
    if amount_paid <= crm_balance:
        return ValidationResult(True)
    if crm_balance <= threshold:
        return ValidationResult(False, "overpayment_balance_too_low")
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


@dataclass
class UnblockPlan:
    """Résultat de la répartition d'un paiement sur plusieurs abonnements.

    - ``macs``          : MAC des abonnements à débloquer (entièrement couverts,
                          + au plus un abo marginal couvert via la tolérance).
    - ``covered_count`` : nombre d'abonnements débloqués.
    - ``total_due``     : somme des prix des abonnements suspendus considérés.
    - ``remainder``     : montant disponible non consommé (→ reste en crédit).
    """
    macs: list[str]
    covered_count: int
    total_due: int
    remainder: int


def plan_unblocks(
    suspended_services: list[dict],
    available: int,
    threshold: int,
) -> UnblockPlan:
    """Répartit ``available`` (montant payé + crédit existant) sur les
    abonnements suspendus et renvoie les MAC à débloquer.

    Règle :
    - On ne considère que les abonnements ayant un MAC exploitable et un prix
      strictement positif (les ``pending-XXXX`` et MAC vides sont ignorés).
    - Les abonnements sont triés par **prix croissant** : on débloque ainsi le
      maximum d'abonnements quand le paiement est partiel (plus favorable au
      client qui a payé pour plusieurs abos).
    - Chaque abonnement entièrement couvert est débloqué (``available`` déduit).
    - L'abonnement marginal (manque ≤ ``threshold``) est couvert **une seule
      fois** grâce à la tolérance, puis on s'arrête. Cela préserve le
      comportement historique (payer 1490 pour un abo à 1500 débloque).
    - Le reliquat (``remainder``) reste disponible en crédit.

    ``suspended_services`` : ``[{"mac": str, "price": int|float}, ...]``.
    """
    items: list[tuple[int, str]] = []
    for svc in suspended_services:
        mac = (svc.get("mac") or "").strip()
        if not mac or mac.lower().startswith("pending-"):
            continue
        try:
            price = int(round(float(svc.get("price"))))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        items.append((price, mac))

    items.sort(key=lambda it: it[0])
    total_due = sum(price for price, _ in items)

    macs: list[str] = []
    remaining = available
    tolerance_used = False
    for price, mac in items:
        if remaining >= price:
            macs.append(mac)
            remaining -= price
        elif not tolerance_used and (price - remaining) <= threshold:
            # Abo marginal couvert grâce à la tolérance (consommée une seule fois).
            macs.append(mac)
            remaining = 0
            tolerance_used = True
            break
        else:
            break

    return UnblockPlan(
        macs=macs,
        covered_count=len(macs),
        total_due=total_due,
        remainder=max(0, remaining),
    )
