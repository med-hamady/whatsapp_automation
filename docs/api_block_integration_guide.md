# Guide d'intégration API — Blocage / déblocage client (FAI)

Ce document décrit comment communiquer avec l'endpoint de **blocage / déblocage**
d'un abonnement client sur le routeur (FAI) : authentification, structure de la
requête et format de la réponse. Il s'adresse à l'équipe qui va consommer l'API.

---

## 1. Présentation

| | |
|---|---|
| Endpoint | `/api/clients/block` |
| Méthode | `POST` |
| Rôle | Bloquer ou débloquer **un abonnement précis** (identifié par sa MAC) d'un client |

- **block** → coupe l'accès Internet de l'abonnement (ajoute une règle de blocage sur le routeur)
- **unblock** → rétablit l'accès (supprime les règles de blocage)

---

## 2. URL et authentification

### URL

```
POST http://13.49.185.225/uisp/whatsapp-py/api/clients/block
```

Les réponses sont au format **JSON** (`Content-Type: application/json`).

### Authentification

L'appel doit inclure une **clé d'API d'action** dans le header HTTP :

| Header | Valeur |
|---|---|
| `X-Admin-Key` | clé d'action (transmise séparément, canal sécurisé) |
| `Content-Type` | `application/json` |

> La clé vous sera communiquée séparément. Ne la stockez jamais en clair dans un
> dépôt de code et ne l'écrivez jamais dans les logs.

Sans clé valide → **`401 Unauthorized`**.

---

## 3. Structure de la requête

### Corps (JSON)

```json
{
  "phone": "31400048",
  "mac": "6c:63:f8:b8:cd:0c",
  "action": "block"
}
```

| Champ | Type | Obligatoire | Description |
|---|---|---|---|
| `phone` | string | oui | Téléphone du client |
| `mac` | string | oui | MAC de l'abonnement ciblé |
| `action` | string | oui | `"block"` ou `"unblock"` |

### Format du téléphone

Le champ `phone` accepte plusieurs formats — l'API les normalise automatiquement :

| Vous envoyez | L'API interprète |
|---|---|
| `31400048` | `31400048` |
| `22231400048` | `31400048` (préfixe pays 222 retiré) |
| `+22231400048` | `31400048` |
| `22231400048@c.us` | `31400048` |

### Règle de sécurité importante

Le `mac` fourni **doit appartenir à un abonnement du `phone`** indiqué. Sinon
l'API répond `404`. On ne peut donc pas agir sur une MAC arbitraire en
connaissant seulement un numéro valide.

➡ Pour connaître les MAC d'un client, utilisez d'abord l'endpoint de
consultation `/api/clients/lookup` (champ `fai[].mac`).

---

## 4. Exemples d'appel

### Bloquer — cURL

```bash
curl -X POST "http://13.49.185.225/uisp/whatsapp-py/api/clients/block" \
  -H "X-Admin-Key: <CLE_ADMIN>" \
  -H "Content-Type: application/json" \
  -d '{"phone":"31400048","mac":"6c:63:f8:b8:cd:0c","action":"block"}'
```

### Débloquer — cURL

```bash
curl -X POST "http://13.49.185.225/uisp/whatsapp-py/api/clients/block" \
  -H "X-Admin-Key: <CLE_ADMIN>" \
  -H "Content-Type: application/json" \
  -d '{"phone":"31400048","mac":"6c:63:f8:b8:cd:0c","action":"unblock"}'
```

### Postman

| Champ | Valeur |
|---|---|
| Méthode | `POST` |
| URL | `http://13.49.185.225/uisp/whatsapp-py/api/clients/block` |
| Header | `X-Admin-Key` = `<CLE_ADMIN>` |
| Header | `Content-Type` = `application/json` |
| Body (raw, JSON) | `{"phone":"31400048","mac":"6c:63:f8:b8:cd:0c","action":"block"}` |

### Python

```python
import httpx

headers = {"X-Admin-Key": "<CLE_ADMIN>"}
url = "http://13.49.185.225/uisp/whatsapp-py/api/clients/block"
payload = {"phone": "31400048", "mac": "6c:63:f8:b8:cd:0c", "action": "block"}

r = httpx.post(url, json=payload, headers=headers, timeout=15)
r.raise_for_status()
print(r.json())
```

---

## 5. Format de la réponse (`200 OK`)

```json
{
  "phone": "31400048",
  "mac": "6c:63:f8:b8:cd:0c",
  "action": "block",
  "rules_changed": 1,
  "statu_local": 2,
  "local_rows_updated": 1,
  "is_blocked": true,
  "block_rule_count": 1
}
```

| Champ | Type | Description |
|---|---|---|
| `phone` | string | Téléphone normalisé |
| `mac` | string | MAC ciblée |
| `action` | string | Action effectuée (`block` / `unblock`) |
| `rules_changed` | int | Règles ajoutées (block) ou supprimées (unblock). `0` = état déjà conforme |
| `statu_local` | int | Nouveau statut : `2` = bloqué, `0` = actif |
| `local_rows_updated` | int | Nombre de lignes mises à jour côté base |
| `is_blocked` | bool | État de blocage **vérifié** sur le routeur après l'action |
| `block_rule_count` | int | Nombre de règles de blocage actives après l'action |

> **Idempotent** : bloquer un client déjà bloqué (ou débloquer un déjà actif)
> renvoie `200` avec `rules_changed: 0`. Vous pouvez rejouer l'appel sans risque
> de doublon.

---

## 6. Codes HTTP et erreurs

| Code | Signification | Action recommandée |
|---|---|---|
| `200` | Action effectuée (ou état déjà conforme) | Traiter la réponse |
| `401` | Clé `X-Admin-Key` absente ou invalide | Vérifier le header |
| `404` | Le `mac` n'appartient pas à ce `phone` | Vérifier le couple phone/mac |
| `422` | `action` invalide, ou `phone`/`mac` manquant | Corriger la requête |
| `502` | Le routeur n'a pas répondu | Réessayer plus tard (le statut n'a pas changé) |

**Cohérence garantie** : l'API agit **d'abord** sur le routeur, puis met à jour
le statut côté base seulement en cas de succès. Si le routeur échoue (`502`), le
statut reste inchangé — pas d'incohérence entre l'affichage et l'état réel.

### Exemples d'erreurs

```json
// 422 — action invalide
{"detail": "action must be 'block' or 'unblock'"}

// 404 — MAC non rattachée au téléphone
{"detail": "mac_not_found_for_phone"}
```

---

## 7. Recommandations d'intégration

- **Timeout client** : prévoir ≥ 10 secondes (l'action interroge le routeur).
- **Retries** : en cas de `502` ou erreur réseau, réessayer avec un délai (pas de boucle serrée). L'endpoint est sûr à rejouer (idempotent).
- **Sécurité** : conserver la clé dans un coffre / variable d'environnement, jamais en clair dans le code ni les logs.
- **Note transport** : l'API est actuellement servie en HTTP (non chiffré). À utiliser depuis le réseau interne / VPN tant que HTTPS n'est pas activé.

---

## 8. Récapitulatif rapide

```
POST /uisp/whatsapp-py/api/clients/block
     Header: X-Admin-Key: <CLE_ADMIN>
     Header: Content-Type: application/json
     Body:   {"phone":"<num>","mac":"<mac>","action":"block"|"unblock"}

→ 200 OK : {... "rules_changed", "is_blocked", "statu_local" ...}
```

Contact : équipe whatsapp_automation.
