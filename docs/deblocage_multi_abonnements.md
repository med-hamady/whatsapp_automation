# Déblocage multi-abonnements sur paiement unique

## Contexte / problème résolu

Un client peut avoir **plusieurs abonnements** (plusieurs services UCRM, donc
plusieurs MAC) sous un **seul compte** (un `idclient`). Quand il paie pour
plusieurs abonnements **en un seul versement** et envoie **une seule capture**,
l'ancien système ne débloquait **qu'un seul abonnement** et laissait le reste de
l'argent en crédit — les autres abonnements restaient bloqués sur le routeur
alors qu'ils étaient payés.

**Cause technique :** le pipeline ne prenait que la 1ʳᵉ ligne client (un MAC),
le job ne portait qu'un MAC, et le statut local passait **tout le compte** à
« actif » alors qu'un seul MAC était débloqué physiquement (la base mentait).

## Ce que fait la nouvelle fonctionnalité

À la réception d'une capture de paiement valide pour un client connu :

1. On récupère **tous les abonnements** du client (toutes les lignes locales +
   tous les services UCRM avec `prix`, `statut`, `MAC`).
2. On calcule le **disponible** = `montant payé` + `crédit existant` sur le compte.
3. On répartit ce disponible sur les abonnements **suspendus**, **triés par prix
   croissant** (on débloque ainsi le plus d'abonnements possible si le paiement
   est partiel).
4. On **débloque chaque abonnement entièrement couvert** (suppression de la règle
   firewall MikroTik sur sa MAC) et on passe **uniquement ces abonnements** à
   « actif » en base — les autres **restent suspendus**.
5. Le **reliquat** non consommé reste en crédit côté UCRM.

### Règles de décision

- **Source de vérité des abonnements à débloquer** : les services UCRM dont le
  statut = `Suspended` (code 3), avec un MAC exploitable et un prix > 0.
- **Tri** : prix croissant (maximise le nombre d'abonnements reconnectés).
- **Tolérance** (`UNDERPAYMENT_TOLERANCE`, 150 MRU par défaut) : un abonnement
  dont le manque est ≤ 150 est quand même débloqué, mais cette tolérance n'est
  utilisée **qu'une seule fois** par paiement (sur l'abonnement marginal).
- **Crédit existant inclus** : l'argent déjà présent en crédit sur le compte
  s'ajoute au montant payé pour décider des déblocages.
- **Reliquat** : tout ce qui dépasse reste en crédit (UCRM l'applique via
  `applyToInvoicesAutomatically`).

## Scénarios gérés

Exemple de référence : client avec 3 abonnements suspendus de prix **1500 /
1500 / 2000** (total dû 5000), sauf mention contraire.

| # | Scénario | Résultat |
|---|----------|----------|
| 1 | **Paiement complet** : paie 5000 | Les **3** abonnements débloqués, reliquat 0 |
| 2 | **Paiement partiel** : paie 3000 | Les **2 moins chers** (1500+1500) débloqués ; l'abo à 2000 reste bloqué ; reste en crédit |
| 3 | **Paiement partiel** : paie 2000 | **1** abo (1500) débloqué ; reliquat 500 en crédit ; les 2 autres restent bloqués |
| 4 | **Crédit existant** : 500 en crédit + paie 1000 (abo à 1500) | disponible 1500 → abo **débloqué** |
| 5 | **Sous-paiement dans la tolérance** : 1 abo à 1500, paie 1490 | **Débloqué** (manque 10 ≤ 150) — comportement historique préservé |
| 6 | **Sous-paiement hors tolérance** : 1 abo à 1500, paie 1349 | **Non débloqué** (manque 151 > 150), paiement enregistré |
| 7 | **Tolérance non cumulable** : 2 abos à 1500, paie 1490 | **1 seul** débloqué (tolérance consommée une fois) |
| 8 | **Tolérance sur le marginal** : 2 abos à 1500, paie 2990 | Les **2** débloqués (1er couvert, 2e via tolérance) |
| 9 | **Sur-paiement** : 1 abo à 1000, paie 1500 | Débloqué, **reliquat 500** en crédit |
| 10 | **Client déjà actif paie en avance** | Aucun déblocage, paiement enregistré, compte à jour |
| 11 | **Aucun versement utile** : paie 0 / rien d'extrait | Rien débloqué |

### Cas techniques / sécurité

| Cas | Comportement |
|-----|--------------|
| **MAC `pending-XXXX` ou vide** (client pas encore provisionné MikroTik) | Ignoré dans l'allocation, aucun appel routeur |
| **Casse du MAC** (UCRM vs base locale) | Rattachement insensible à la casse ; on utilise la casse de la base locale pour le statut |
| **Services UCRM indisponibles** (timeout/5xx) | **Repli mono-abonnement** : règle historique (statut local + solde agrégé) sur l'abonnement principal |
| **UCRM injoignable pour les détails (solde)** | Paiement **non traité**, notification support, on attend qu'UCRM remonte |
| **Anti sur-paiement** : payé > dû et solde restant ≤ 150 | **Refusé** (probable capture rejouée ou erreur client) |
| **Idempotence** : même `txn_id` rejoué | Ignoré (déjà traité ou déjà en queue), pas de double paiement |
| **Reprise sur incident** (worker) | Étapes idempotentes repérées par `step_done` ; un crash en cours ne rejoue pas une étape déjà faite |

## Le correctif clé : statut par MAC

Avant, le worker passait **toutes** les lignes du client à « actif » alors qu'un
seul MAC était débloqué → incohérence base / routeur. Désormais le statut est mis
à jour **par MAC** (`update_client_status_by_mac`) : seuls les abonnements
réellement débloqués passent à `statu=0`, les non couverts restent `statu=2`.

## Message envoyé au client

- Plusieurs abonnements réactivés : « ✅ Paiement reçu, N abonnements réactivés. »
- Un seul : « ✅ Paiement reçu, votre connexion est réactivée. »
- Paiement enregistré mais incomplet : « ⚠ Paiement enregistré mais incomplet. »

Le détail montants (total dû / payé / reste / avoir éventuel) est toujours joint.

## Fichiers modifiés

- `src/whatsapp_automation/worker/ucrm.py` — `get_client_services()` expose `mac` + `ip`.
- `src/whatsapp_automation/webhook/validators.py` — fonction pure `plan_unblocks()`.
- `src/whatsapp_automation/models/job.py` — champ `unblock_macs` (défaut vide, rétro-compatible).
- `src/whatsapp_automation/webhook/pipeline.py` — tous les abonnements + services UCRM + planification.
- `src/whatsapp_automation/worker/handlers.py` — déblocage par MAC + statut par MAC.

## Tests

- `python scripts/test_unblock_allocation.py` → **13/13 PASS** (allocation multi-abos).
- `python scripts/test_subsequent_payment_rule.py` → **12/12 PASS** (anti sur-paiement, non-régression).

## Compatibilité / déploiement

- Pas de migration SQL : le job est sérialisé en JSON, le nouveau champ a un
  défaut vide → les jobs déjà en queue restent valides (repli sur l'ancien MAC).
- Nécessite le **redémarrage des services webhook + worker** (NSSM) pour charger
  le code.
