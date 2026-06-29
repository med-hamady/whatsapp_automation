"""Détecteur des fiches « NOUVEL ABONNEMENT » de Connect A2.

Ce ne sont PAS des reçus de paiement mais des formulaires d'inscription d'un
nouveau client (nom, adresse, forfait, date d'installation, signature) qu'un
client envoie parfois par erreur sur le même canal WhatsApp.

Avant ce détecteur, ces fiches étaient classées `sedad` : leur clause légale
« ...après résiliation ou non-paiement » fait matcher le marqueur "paiement"
de SedadExtractor (0.2), seul extracteur à scorer dessus. L'OCR extrayait
alors un faux montant (souvent le N° WhatsApp ou le NNI du formulaire).

On les classe explicitement `subscription_form` avec un montant nul : le
webhook (validate_document_type) les écarte ensuite sans tenter de paiement.
"""

from __future__ import annotations

import re

from .base import BaseExtractor, ExtractionResult


# Marqueurs propres aux fiches d'abonnement Connect A2. Match insensible à la
# casse et tolérant aux espaces/ponctuation que l'OCR insère ou retire
# (`\s*` entre les mots). On exige AU MOINS DEUX marqueurs distincts pour
# conclure : un vrai reçu n'en contient quasi jamais deux à la fois, alors que
# la fiche en aligne une dizaine. Garde la même logique que
# webhook.validators.validate_document_type (défense en profondeur).
_MARKERS: tuple[str, ...] = (
    r"NOUVEL\s*ABONNEMENT",
    r"FORFAIT\s*/?\s*PACKAGE",
    r"AIR\s*FIBER",
    r"DATE\s*D[' ]?\s*INSTALLATION",
    r"SERVICE\s*DISTRIBUTION",
    r"RAISON\s*SOCIALE",
    r"IDENTIFICATION\s*DU\s*CLIENT",
    r"NOMBRES?\s*D[' ]?\s*ETAGES",
)
_MIN_HITS = 2


class SubscriptionFormExtractor(BaseExtractor):
    name = "subscription_form"

    def detect(self, text: str) -> float:
        hits = sum(
            1 for pattern in _MARKERS if re.search(pattern, text, re.IGNORECASE)
        )
        if hits < _MIN_HITS:
            return 0.0
        # Score franc (>= 0.7) pour battre tout extracteur de reçu (ex : Sedad
        # à 0.2 sur "non-paiement"). Croît avec le nombre de marqueurs.
        return min(1.0, 0.5 + 0.1 * hits)

    def extract(self, text: str, ocr_result=None) -> ExtractionResult:
        # Aucune extraction : ce n'est pas un paiement. On renvoie un résultat
        # vide (montant/txn/date à None) avec le bon template.
        return ExtractionResult(template=self.name, detect_score=self.detect(text))
