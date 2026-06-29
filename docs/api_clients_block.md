# API Endpoint : Blocage / déblocage client sur le FAI

> Document technique destiné à l'équipe IT / intégration.
> Version 1.0 — 2026-06-11

---

## 1. Objectif

Endpoint **HTTP POST (action d'écriture)** qui bloque ou débloque un abonnement
(identifié par sa MAC) d'un client sur le routeur Mikrotik (FAI).

- **block** → ajoute une règle firewall DROP pour la MAC (coupe l'accès) + passe le statut local à `2` (suspendu)
- **unblock** → supprime les règles DROP de la MAC (rétablit l'accès) + passe le statut local à `0` (actif)

⚠ Action sensible : elle coupe/rétablit l'accès Internet de vrais clients.

---

## 2. URL et authentification

```
POST http://13.49.185.225/uisp/whatsapp-py/api/clients/block
```

| Header | Valeur |
|---|---|
| `X-Admin-Key` | clé **ADMIN** dédiée (distincte de la clé de consultation) |
| `Content-Type` | `application/json` |

La clé est définie côté serveur dans `.env` (variable `ADMIN_API_KEY`). Elle est
**volontairement différente** de la clé `CLIENT_API_KEY` (consultation) : ainsi,
la clé de lecture seule ne permet pas de bloquer/débloquer un client.

Sans header `X-Admin-Key` valide → **`401 Unauthorized`**.

---

## 3. Corps de la requête (JSON)

```json
{
  "phone": "31400048",
  "mac": "6c:63:f8:b8:cd:0c",
  "action": "block"
}
```

| Champ | Type | Obligatoire | Description |
|---|---|---|---|
| `phone` | string | oui | Téléphone du client (normalisé automatiquement) |
| `mac` | string | oui | MAC de l'abonnement à bloquer/débloquer |
| `action` | string | oui | `"block"` ou `"unblock"` |

**Sécurité** : le `mac` fourni doit appartenir à un abonnement rattaché au
`phone` donné, sinon `404`. On ne peut donc pas bloquer une MAC arbitraire du
réseau en connaissant juste un numéro valide.

Pour connaître les MAC d'un client, utiliser l'endpoint de consultation
`/api/clients/lookup` (champ `fai[].mac`).

---

## 4. Réponse (`200 OK`)

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
| `mac` | string | MAC ciblée (casse telle que stockée en base) |
| `action` | string | Action effectuée (`block` / `unblock`) |
| `rules_changed` | int | Règles firewall ajoutées (block) ou supprimées (unblock). `0` = déjà dans l'état voulu (idempotent) |
| `statu_local` | int | Nouveau statut local : `2` = bloqué, `0` = actif |
| `local_rows_updated` | int | Nombre de lignes mises à jour en base locale |
| `is_blocked` | bool | État de blocage **vérifié** sur le routeur après l'action |
| `block_rule_count` | int | Nombre de règles DROP actives pour la MAC après l'action |

**Idempotence** : bloquer un client déjà bloqué (ou débloquer un déjà actif)
renvoie `200` avec `rules_changed: 0` — pas d'erreur, pas de doublon de règle.

---

## 5. Codes HTTP

| Code | Quand |
|---|---|
| **200** | Action effectuée (ou déjà dans l'état voulu) |
| **401** | Header `X-Admin-Key` absent ou invalide |
| **404** | Le `mac` n'appartient pas à un abonnement de ce `phone` |
| **422** | `action` ≠ block/unblock, ou `mac`/`phone` manquant |
| **502** | Le routeur Mikrotik a échoué — **le statut local n'est PAS modifié** (pas d'incohérence) |

L'ordre des opérations garantit la cohérence : on agit **d'abord** sur le
routeur, et seulement en cas de succès on aligne le statut local. Si le routeur
échoue, la base locale reste inchangée.

---

## 6. Exemples

### cURL — bloquer

```bash
curl -X POST "http://13.49.185.225/uisp/whatsapp-py/api/clients/block" \
  -H "X-Admin-Key: <ADMIN_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"phone":"31400048","mac":"6c:63:f8:b8:cd:0c","action":"block"}'
```

### cURL — débloquer

```bash
curl -X POST "http://13.49.185.225/uisp/whatsapp-py/api/clients/block" \
  -H "X-Admin-Key: <ADMIN_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"phone":"31400048","mac":"6c:63:f8:b8:cd:0c","action":"unblock"}'
```

### Postman

| Champ | Valeur |
|---|---|
| Méthode | `POST` |
| URL | `http://13.49.185.225/uisp/whatsapp-py/api/clients/block` |
| Header | `X-Admin-Key` = `<ADMIN_KEY>` |
| Header | `Content-Type` = `application/json` |
| Body (raw JSON) | `{"phone":"...","mac":"...","action":"block"}` |

---

## 7. Implémentation

- Route : `src/whatsapp_automation/webhook/app.py::block_client`
- Blocage routeur : `src/whatsapp_automation/worker/mikrotik.py::block_by_mac` (règle `chain=forward action=drop place-before=0`, réplique de `add_rules.php`)
- Déblocage routeur : `mikrotik.py::unblock_by_mac` (déjà utilisé par le worker de paiement)
- Statut local : `src/whatsapp_automation/db/postgres.py::update_client_status_by_mac` (ciblé par MAC)
- Service : `whatsapp_webhook` (port `127.0.0.1:8010`), exposé via Apache (`httpd-whatsapp-py.conf`)

### Sécurité — rotation de clé

En cas de fuite de la clé admin :
1. `python -c "import secrets; print(secrets.token_hex(32))"`
2. Mettre à jour `ADMIN_API_KEY` dans `.env`
3. `Restart-Service whatsapp_webhook`

⚠ Même limite que l'endpoint de consultation : HTTP non chiffré → la clé
transite en clair. À réserver au réseau interne tant que TLS n'est pas en place.
