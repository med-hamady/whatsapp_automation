# Déblocage via le superviseur réseau (API FAI / blocage sur le LR)

> Intégration du **second mécanisme de coupure** dans le système de paiement.
> Version 1.0 — 2026-07-14

---

## 1. Ce qui change

Jusqu'ici, couper un client = poser une règle firewall `drop` sur la **MAC** dans
le **routeur core MikroTik**. L'équipe réseau a ajouté un second mécanisme : le
blocage est appliqué **directement sur le LR du client** (en SSH), persisté côté
superviseur et **ré-appliqué toutes les 120 s** — il survit donc à un reboot du LR.

Les deux mécanismes **coexistent** : un client peut avoir été coupé par l'un,
l'autre, ou les deux. Le système de paiement lève donc **systématiquement les
deux** quand un paiement est validé. Les deux sont idempotents, lever une coupure
absente ne coûte rien.

> **Le système de paiement ne bloque pas.** Poser une coupure (`POST /fai/block`,
> modes `full` / `whatsapp_only`) reste la prérogative du superviseur et de
> l'équipe réseau. Côté paiement on ne fait que **débloquer** et **lire l'état** :
> `fai_supervisor.py` n'expose volontairement pas de fonction de blocage.

| | Firewall MikroTik | Superviseur LR |
|---|---|---|
| Où | Routeur core | LR du client (SSH) |
| Clé | MAC | MAC |
| Survit au reboot du LR | oui | oui (ré-application 120 s) |
| Posé par | équipe réseau | superviseur |
| Levé par le paiement | oui | oui |
| Code | `worker/mikrotik.py` | `worker/fai_supervisor.py` |

---

## 2. Configuration (`.env`)

```ini
FAI_API_BASE_URL=https://102.215.95.233
FAI_API_KEY=<clé fournie par l'équipe réseau>
FAI_API_TIMEOUT=90
FAI_API_STATUS_TIMEOUT=15
FAI_API_VERIFY_SSL=false
```

- **`FAI_API_TIMEOUT` ≥ 60 s : exigé par l'équipe réseau.** L'appel `unblock`
  attend la réponse **réelle du LR du client** avant de répondre — c'est ce qui
  rend le résultat fiable. Avec un timeout court (10-20 s), on conclurait à un
  échec alors que l'ordre a bel et bien été exécuté. On est à 90 s.
- `FAI_API_STATUS_TIMEOUT` (15 s) est distinct : `status` ne sollicite pas le LR,
  et il est appelé en direct par `/api/clients/lookup` — un opérateur ne doit pas
  attendre 90 s devant une fiche client si le superviseur rame.

- **`FAI_API_KEY` est le seul secret qui protège l'accès des clients** : l'API est
  joignable depuis Internet et il n'y a pas de filtrage par IP source. Jamais dans
  git, jamais dans une URL. En cas de doute sur une fuite → prévenir l'équipe
  réseau, la clé est révoquée et remplacée en une minute.
- `FAI_API_VERIFY_SSL=false` : le superviseur présente un **certificat auto-signé**.
  La vérification TLS est désactivée **pour cet hôte uniquement** ; la connexion
  reste chiffrée. Ne passer à `true` que le jour où un vrai certificat est posé.
- **URL ou clé vide → mécanisme désactivé** (no-op, aucun appel réseau) : on
  retombe exactement sur le comportement d'avant (MikroTik seul). C'est le défaut
  en dev et dans les tests.

Après modification du `.env` : `Restart-Service whatsapp_worker` et
`Restart-Service whatsapp_webhook`.

---

## 3. Où c'est branché

### a. Worker de paiement — `worker/handlers.py::_unblock_mac`

Étape `unblocked` du job. Pour **chaque MAC** à débloquer (un paiement peut couvrir
plusieurs abonnements) :

1. `mikrotik.unblock_by_mac(mac)` — suppression des règles drop ;
2. `fai_supervisor.unblock_by_mac(mac)` — déblocage du LR.

**Une erreur du superviseur ne fait jamais échouer le job.** Quand on arrive à cette
étape, le paiement est **déjà encaissé en UCRM** : faire échouer le job le ferait
rejouer inutilement. L'erreur est loggée (`ERROR ... Superviseur LR unblock ECHEC`)
et visible sur le dashboard ; le client reçoit quand même son reçu.

### b. Endpoint admin — `POST /api/clients/block`

`action: "unblock"` lève les deux coupures (MikroTik + superviseur) et renvoie la
réponse du superviseur dans `supervisor`. `action: "block"` n'agit **que** sur le
firewall MikroTik (`supervisor: null`) — on ne pose pas de coupure sur le LR
depuis le système de paiement. Détail : `api_clients_block.md`.

### c. Consultation — `GET /api/clients/lookup`

`fai[]` porte désormais l'état des deux mécanismes :

| Champ | Sens |
|---|---|
| `is_blocked` | **Vrai dès que l'un des deux** bloque le client (= le client n'a pas Internet) |
| `blocked_mikrotik` | Bloqué par une règle firewall du routeur core |
| `blocked_supervisor` | Bloqué par le superviseur (intention en base : `client_blocked`) |
| `block_mode` | `full` / `whatsapp_only` / `null` |
| `block_rule_count` | Nombre de règles drop MikroTik (inchangé) |

---

## 4. Le piège : `ok: false` n'est PAS un échec

Le champ `ok` de la réponse superviseur reflète l'application **immédiate sur le
LR**, pas la prise en compte de la demande. Si le LR est momentanément injoignable
(client éteint, radio coupée), la réponse est `HTTP 200` avec `ok: false` et
`client_blocked: true` : **l'ordre est enregistré** et un job le ré-applique dès que
le LR revient.

Règle appliquée dans le code :

- `client_blocked` → l'intention en base, c'est ça qui fait foi ;
- `ok: false` + `retry_scheduled: true` → on logge « application différée », **on ne rejoue pas**.

Aucun retry n'est implémenté côté paiement : le superviseur s'en charge.

## 5. Ce qu'il faut signaler à l'équipe réseau

Trois cas, et **trois seulement**, demandent une intervention humaine. Ils sont tous
loggés en `ERROR` (donc visibles sur le dashboard) :

| Cas | Signification | Pourquoi c'est grave |
|---|---|---|
| `unenforceable_reason` renseigné (HTTP 200) | Le LR refuse la connexion | **Aucun rattrapage automatique** : le client a payé et reste coupé. C'est le cas le plus sournois — la réponse est un 200 |
| `404` | MAC inconnue du parc supervisé | Ne se résoudra jamais seul |
| `409` | Équipement mal configuré (bridge) | Reconfiguration du LR nécessaire |

Chercher dans les logs : `Superviseur LR NON APPLICABLE` et `Superviseur LR unblock ECHEC`.

Les autres erreurs (`400` MAC mal formée, `401`/`403` clé invalide, `5xx` panne
superviseur) sont des bugs ou des incidents de notre côté : elles sont loggées en
`ERROR` mais ne concernent pas l'équipe réseau. Dans tous les cas, le **déblocage
MikroTik a bien eu lieu** et le client a reçu son reçu.

Débit toléré : **120 requêtes/minute**. Les demandes en rafale sont acceptées
immédiatement puis appliquées progressivement — largement au-dessus d'un usage
normal (un appel par transaction).

## 5 bis. Pas d'environnement de test

**Les appels agissent sur de vrais clients.** Il n'existe pas de bac à sable.
Seul `GET /status` est sans effet — c'est le seul appel à utiliser pour valider
une intégration ou une clé.

---

## 6. Tester

```bash
# Lecture seule — ne touche pas au LR
python scripts/test_fai_supervisor.py status d0:21:f9:f6:07:c2

# Déblocage réel (sans effet si le client est déjà actif)
python scripts/test_fai_supervisor.py unblock d0:21:f9:f6:07:c2
```

⚠ Ce script tape sur le **vrai** superviseur. `status` est sans risque ;
`unblock` rétablit réellement l'accès du client visé.

Formats de MAC acceptés (normalisés côté serveur) : `d0:21:f9:f6:07:c2`,
`D0-21-F9-F6-07-C2`, `d021.f9f6.07c2`, `d021f9f607c2`.
