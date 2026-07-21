# Association / confirmation d'un client introuvable sans txn_id

## Contexte / problème résolu

Dans le dashboard, un paiement reçu d'un numéro WhatsApp non reconnu est stocké
dans la table `numeros_introuvable` en attente d'un rattachement manuel à un
client (identifiant CRM). Deux étapes permettent de le résoudre :

1. **Association** (`POST /dashboard/api/unknown-clients/{id}/associate`) :
   l'admin saisit l'identifiant CRM du client.
2. **Confirmation** (`POST /dashboard/api/unknown-clients/{id}/confirm`) : le
   système relit PostgreSQL/UCRM à chaud et met le paiement en file d'attente,
   exactement comme un paiement webhook normal.

Ces deux routes refusaient systématiquement (409) tout ticket dont le
`txn_id` était vide, avec le message *"Ce reçu ne contient pas de txn_id
fiable. Traitement automatique refusé."* Le champ était aussi désactivé côté
interface, avec un avertissement bloquant la saisie.

Le problème : **certains opérateurs de paiement (masrivi, generic) n'ont
jamais de txn_id extractible** — ce n'est pas une anomalie, c'est une
caractéristique connue et déjà gérée du système (voir le commentaire dans
`jobqueue/schema.sql` : *"txn_id != '' car certains opérateurs (masrvi,
generic) n'ont pas de txn_id extractible"*). Le flux webhook normal traite
ces paiements sans aucun problème tous les jours : la queue accepte plusieurs
jobs à txn_id vide en parallèle (l'index unique exclut explicitement les
lignes à txn_id vide), et le modèle `Payment` accepte un txn_id vide comme
une valeur normale.

Résultat concret avant le correctif : un paiement masrivi ou generic tombé en
"client introuvable" restait bloqué **pour toujours** — impossible à associer
ni à confirmer manuellement, alors que ce même paiement, si le client avait
été reconnu du premier coup, se serait traité sans aucun souci.

## Ce que fait le correctif

- Suppression du blocage sur `txn_id` manquant dans `associate()` et dans
  `confirm()` (`webhook/dashboard/routes.py`).
- Suppression de l'avertissement et de la désactivation du champ côté
  interface (`dashboard.html`) — la saisie de l'identifiant CRM est
  disponible dès que le ticket est `pending`, avec ou sans txn_id.
- Le `txn_id` vide continue de circuler tel quel jusqu'au `Job` final
  (`job_builder.build_job` le normalisait déjà en chaîne vide, ce
  comportement existait avant le correctif et n'a pas eu besoin d'être
  modifié).

Aucun autre comportement ne change : les gates restants (montant manquant,
numéro manquant, identifiant client manquant, statut invalide, réservation
déjà en cours...) sont toujours en place, ils protègent contre des cas
réellement dangereux (créer un paiement à 0 MRU, perdre le numéro de
destination du reçu, etc.) — le txn_id n'en fait pas partie.

## Pourquoi ce n'est pas un risque

Le `txn_id` sert uniquement à éviter de traiter deux fois le même paiement
(déduplication). Pour les paiements qui en ont un, cette protection continue
de fonctionner normalement. Pour les paiements qui n'en ont jamais eu (toute
une catégorie d'opérateurs), l'absence de déduplication par txn_id n'est pas
une régression introduite ici : c'est exactement la situation déjà acceptée
et en production aujourd'hui pour ces mêmes paiements quand ils passent par
le flux webhook normal. Le dashboard ne fait plus qu'appliquer la même règle,
au lieu d'être plus strict que le reste du système sans raison.

## Fichiers modifiés

- `src/whatsapp_automation/webhook/dashboard/routes.py` — retrait des deux
  gates 409 (`associate`, `confirm`).
- `src/whatsapp_automation/webhook/dashboard/templates/dashboard.html` —
  retrait de l'avertissement et de la désactivation du champ d'identifiant
  CRM liés au txn_id manquant.
- `scripts/test_unknown_clients_associate.py` — le cas "sans txn_id"
  vérifie désormais une association réussie (200) au lieu d'un refus.
- `scripts/test_unknown_clients_confirm.py` — le cas "sans txn_id" vérifie
  désormais une confirmation réussie (200, statut `queued`, job récupérable
  en file) au lieu d'un refus.

## Tests

- `python scripts/test_unknown_clients_associate.py`
- `python scripts/test_unknown_clients_confirm.py`
- `python scripts/test_unknown_clients_dashboard_api.py`
- `python scripts/test_pipeline_phone_resolution_fallback.py`

## Compatibilité / déploiement

Aucune migration de base de données. Aucun changement de schéma. Nécessite
uniquement le redémarrage du service webhook (dashboard) pour charger le
nouveau code des routes et le template mis à jour.
