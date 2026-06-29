# API — Blocage / déblocage client (FAI)

Bloquer ou débloquer un abonnement (par sa MAC) sur le routeur.

## Requête

```
POST http://13.49.185.225/uisp/whatsapp-py/api/clients/block
```

**Headers**
```
X-Admin-Key: <CLE_ADMIN>
Content-Type: application/json
```

**Body (JSON)**
```json
{
  "phone": "31400048",
  "mac": "6c:63:f8:b8:cd:0c",
  "action": "block"
}
```

| Champ | Description |
|---|---|
| `phone` | Téléphone du client |
| `mac` | MAC de l'abonnement (doit appartenir au client) |
| `action` | `"block"` ou `"unblock"` |

## Réponse (`200 OK`)

```json
{
  "phone": "31400048",
  "mac": "6c:63:f8:b8:cd:0c",
  "action": "block",
  "rules_changed": 1,
  "statu_local": 2,
  "is_blocked": true,
  "block_rule_count": 1
}
```

| Champ | Description |
|---|---|
| `action` | Action effectuée |
| `rules_changed` | Règles modifiées (`0` = état déjà conforme) |
| `statu_local` | `2` = bloqué, `0` = actif |
| `is_blocked` | État vérifié sur le routeur après l'action |

## Codes d'erreur

| Code | Signification |
|---|---|
| `401` | Clé `X-Admin-Key` absente ou invalide |
| `404` | La MAC n'appartient pas au téléphone |
| `422` | Champ manquant ou `action` invalide |
| `502` | Routeur injoignable (statut non modifié) |

## Exemple cURL

```bash
curl -X POST "http://13.49.185.225/uisp/whatsapp-py/api/clients/block" \
  -H "X-Admin-Key: <CLE_ADMIN>" \
  -H "Content-Type: application/json" \
  -d '{"phone":"31400048","mac":"6c:63:f8:b8:cd:0c","action":"block"}'
```

> Idempotent : rejouer l'appel est sans risque. Pour obtenir la MAC d'un client,
> utiliser `/api/clients/lookup` (champ `fai[].mac`).
