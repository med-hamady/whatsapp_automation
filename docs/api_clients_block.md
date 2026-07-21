# API Endpoint : Blocage / déblocage client sur le FAI

> Document technique destiné à l'équipe IT / intégration.
> Version 1.1 — 2026-07-14 (ajout du superviseur LR — cf. `deblocage_superviseur_lr.md`)

---

## 1. Objectif

Endpoint **HTTP POST (action d'écriture)** qui bloque ou débloque un abonnement
(identifié par sa MAC) d'un client.

Deux mécanismes de coupure coexistent sur le réseau : le **firewall MikroTik**
(routeur core) et le **superviseur LR** (coupure posée sur le LR du client, en SSH).

- **block** → règle firewall DROP MikroTik + statut local à `2` (suspendu).
  ⚠ **Ne pose aucune coupure sur le LR** : bloquer via le superviseur est la
  prérogative de l'équipe réseau, pas du système de paiement.
- **unblock** → suppression des règles DROP **et** déblocage du LR (le client a pu
  être coupé par l'un ou par l'autre) + statut local à `0` (actif)

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

Exemple sur un `unblock` (le seul cas où le superviseur est appelé) :

```json
{
  "phone": "31400048",
  "mac": "6c:63:f8:b8:cd:0c",
  "action": "unblock",
  "rules_changed": 1,
  "statu_local": 0,
  "local_rows_updated": 1,
  "is_blocked": false,
  "block_rule_count": 0,
  "supervisor": {
    "ok": true,
    "message": "Client 36086261-Toutoumedlimam débloqué. Interface eth0.1 remontée.",
    "mac": "6c:63:f8:b8:cd:0c",
    "name": "36086261-Toutoumedlimam",
    "client_blocked": false,
    "block_mode": null,
    "client_block_enforced_at": "2026-07-14T10:32:11.482Z",
    "error": null
  }
}
```

| Champ | Type | Description |
|---|---|---|
| `phone` | string | Téléphone normalisé |
| `mac` | string | MAC ciblée (casse telle que stockée en base) |
| `action` | string | Action effectuée (`block` / `unblock`) |
| `rules_changed` | int | Règles firewall **MikroTik** ajoutées (block) ou supprimées (unblock). `0` = déjà dans l'état voulu (idempotent) |
| `statu_local` | int | Nouveau statut local : `2` = bloqué, `0` = actif |
| `local_rows_updated` | int | Nombre de lignes mises à jour en base locale |
| `is_blocked` | bool | État de blocage **vérifié sur le routeur MikroTik** après l'action |
| `block_rule_count` | int | Nombre de règles DROP MikroTik actives pour la MAC après l'action |
| `supervisor` | objet\|null | Réponse du superviseur LR sur le déblocage. **`null` sur un `block`** (le superviseur n'est jamais sollicité pour couper) et si le superviseur n'est pas configuré. `error` non-null = l'appel a échoué (le déblocage MikroTik, lui, a bien eu lieu) |

**`supervisor.ok: false` n'est pas un échec** : le LR est momentanément
injoignable, mais l'ordre est enregistré côté superviseur et sera ré-appliqué
automatiquement (toutes les 120 s). C'est `supervisor.client_blocked` qui porte
l'intention. Ne pas rejouer l'appel.

**Idempotence** : bloquer un client déjà bloqué (ou débloquer un déjà actif)
renvoie `200` avec `rules_changed: 0` — pas d'erreur, pas de doublon de règle.
Les deux mécanismes sont idempotents.

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

Une panne du **superviseur LR** ne renvoie pas 502 : l'action MikroTik est déjà
appliquée, l'annuler serait pire. L'erreur est loggée et remontée dans
`supervisor.error` (avec `supervisor.http_status`). Les cas `404` (MAC hors parc
supervisé) et `409` (LR en mode bridge) demandent une action de l'équipe réseau,
pas un retry.

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
- Superviseur LR : `src/whatsapp_automation/worker/fai_supervisor.py` (config `FAI_API_BASE_URL` / `FAI_API_KEY`) — cf. `deblocage_superviseur_lr.md`
- Statut local : `src/whatsapp_automation/db/postgres.py::update_client_status_by_mac` (ciblé par MAC)
- Service : `whatsapp_webhook` (port `127.0.0.1:8010`), exposé via Apache (`httpd-whatsapp-py.conf`)

### Sécurité — rotation de clé

En cas de fuite de la clé admin :
1. `python -c "import secrets; print(secrets.token_hex(32))"`
2. Mettre à jour `ADMIN_API_KEY` dans `.env`
3. `Restart-Service whatsapp_webhook`

⚠ Même limite que l'endpoint de consultation : HTTP non chiffré → la clé
transite en clair. À réserver au réseau interne tant que TLS n'est pas en place.
