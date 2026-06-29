# Guide d'intégration API — Clients FAI

Ce document décrit comment communiquer avec l'API : authentification, structure
des requêtes et format des réponses. Il s'adresse à l'équipe qui va consommer
l'API.

Deux endpoints sont disponibles :

| # | Endpoint | Méthode | Rôle |
|---|---|---|---|
| 1 | `/api/clients/lookup` | `GET` | Consulter les informations d'un client (lecture seule) |
| 2 | `/api/clients/block` | `POST` | Bloquer / débloquer un abonnement d'un client |

---

## 1. Généralités

### URL de base

```
http://13.49.185.225/uisp/whatsapp-py
```

Tous les appels se font sur cette base. Les réponses sont au format **JSON**
(`Content-Type: application/json`).

### Authentification

L'API utilise des **clés d'API** passées dans un header HTTP. Il existe deux clés
distinctes, selon le type d'opération :

| Header | Endpoint | Type d'opération |
|---|---|---|
| `X-API-Key` | `/api/clients/lookup` | Lecture (consultation) |
| `X-Admin-Key` | `/api/clients/block` | Écriture (action sensible) |

> Les clés vous seront transmises séparément (canal sécurisé). Ne les stockez
> jamais en clair dans un dépôt de code ; ne les écrivez jamais dans les logs.

Sans clé valide, l'API répond **`401 Unauthorized`**.

### Format du numéro de téléphone

Le paramètre `phone` accepte plusieurs formats — l'API les normalise
automatiquement :

| Vous envoyez | L'API interprète |
|---|---|
| `33848414` | `33848414` |
| `22233848414` | `33848414` (préfixe pays 222 retiré) |
| `+22233848414` | `33848414` |
| `22233848414@c.us` | `33848414` |

---

## 2. Endpoint 1 — Consultation client (`GET /api/clients/lookup`)

Retourne les informations consolidées d'un client à partir de son téléphone :
identité CRM, forfaits d'abonnement, dernières factures, et état réseau (FAI) de
chaque équipement.

### Requête

```
GET /uisp/whatsapp-py/api/clients/lookup?phone=33848414
```

| Élément | Valeur |
|---|---|
| Méthode | `GET` |
| Paramètre query `phone` | **obligatoire** — numéro du client |
| Header `X-API-Key` | **obligatoire** — clé de consultation |

#### Exemple cURL

```bash
curl -H "X-API-Key: <CLE_LECTURE>" \
  "http://13.49.185.225/uisp/whatsapp-py/api/clients/lookup?phone=33848414"
```

### Réponse — client trouvé (`200 OK`)

```json
{
  "phone": "33848414",
  "found": true,
  "crm": {
    "id": 10,
    "client_type": 1,
    "first_name": "Ali",
    "last_name": "Brahim",
    "phone": "33848414",
    "balance": 0,
    "registration_date": "2022-10-18T00:00:00+0000",
    "is_active": false,
    "account_balance": 0.0,
    "account_credit": 0.0,
    "account_outstanding": 0.0,
    "has_suspended_service": false
  },
  "services_count": 1,
  "services": [
    {
      "id": 1800,
      "name": "AirFiber 15Mb",
      "type": "Internet",
      "status": 1,
      "status_label": "Active",
      "price": 990.0,
      "currency": "MRU",
      "download_speed_mb": 20.0,
      "upload_speed_mb": 10.0,
      "active_from": "2022-10-18T00:00:00+0000",
      "active_to": null,
      "last_invoiced_date": "2026-06-30T00:00:00+0000",
      "prepaid": false,
      "has_outage": false
    }
  ],
  "recent_invoices": [
    {
      "id": 24314,
      "number": "024311",
      "created_date": "2026-05-26T06:00:02+0000",
      "due_date": "2026-05-26T06:00:02+0000",
      "total": 990.0,
      "amount_paid": 990.0,
      "amount_to_pay": 0.0,
      "currency": "MRU",
      "status": 3,
      "status_label": "Paid"
    }
  ],
  "fai_count": 1,
  "fai": [
    {
      "mac": "d0:21:f9:af:19:78",
      "ip": "10.135.1.168/16",
      "statu_local": 0,
      "is_blocked": false,
      "block_rule_count": 0,
      "error": null
    }
  ],
  "errors": {
    "crm": null,
    "services": null,
    "invoices": null,
    "fai": null
  }
}
```

### Description des champs

#### Racine

| Champ | Type | Description |
|---|---|---|
| `phone` | string | Numéro normalisé |
| `found` | bool | `true` si le client existe, sinon `false` |
| `crm` | object \| null | Identité et compte CRM |
| `services_count` | int \| null | Nombre de forfaits |
| `services` | array \| null | Détail des forfaits d'abonnement |
| `recent_invoices` | array \| null | Les 5 dernières factures (plus récente d'abord) |
| `fai_count` | int \| null | Nombre d'abonnements/équipements (MAC) |
| `fai` | array \| null | État réseau de chaque équipement |
| `errors` | object | Erreur éventuelle par source (voir §4) |

#### Bloc `crm`

| Champ | Type | Description |
|---|---|---|
| `id` | int | Identifiant client CRM |
| `client_type` | int | `1` = résidentiel, `2` = entreprise |
| `first_name` / `last_name` | string | Nom du client |
| `phone` | string | Téléphone enregistré au CRM |
| `balance` | int | Solde dû (MRU, entier) |
| `registration_date` | date ISO 8601 | Date d'inscription |
| `is_active` | bool | A au moins un service actif |
| `account_balance` | float | Solde du compte |
| `account_credit` | float | Crédit disponible |
| `account_outstanding` | float | Montant dû |
| `has_suspended_service` | bool | A un service suspendu |

#### Bloc `services` (liste — un client peut avoir plusieurs forfaits)

| Champ | Type | Description |
|---|---|---|
| `id` | int | ID du service |
| `name` | string | Nom du forfait (ex : `"AirFiber 15Mb"`) |
| `type` | string | Type (`"Internet"`, `"IPTV"`, …) |
| `status` / `status_label` | int / string | Statut du service (voir tableau §5) |
| `price` | float | Prix mensuel |
| `currency` | string | Devise |
| `download_speed_mb` / `upload_speed_mb` | float | Débits (Mb/s) |
| `active_from` / `active_to` | date \| null | Période d'activité |
| `last_invoiced_date` | date \| null | Dernière facturation |
| `prepaid` | bool | Forfait prépayé |
| `has_outage` | bool | Panne en cours |

#### Bloc `recent_invoices` (liste — 5 dernières factures)

| Champ | Type | Description |
|---|---|---|
| `id` | int | ID de la facture |
| `number` | string | Numéro de facture |
| `created_date` / `due_date` | date ISO 8601 | Émission / échéance |
| `total` | float | Montant total |
| `amount_paid` | float | Montant payé |
| `amount_to_pay` | float | Reste à payer (`0` = soldée) |
| `currency` | string | Devise |
| `status` / `status_label` | int / string | Statut facture (voir tableau §5) |

#### Bloc `fai` (liste — un objet par abonnement / équipement)

| Champ | Type | Description |
|---|---|---|
| `mac` | string | Adresse MAC de l'équipement |
| `ip` | string \| null | IP locale (format CIDR) |
| `statu_local` | int | `0` = actif, `2` = suspendu (base locale) |
| `is_blocked` | bool | `true` si bloqué sur le routeur |
| `block_rule_count` | int | Nombre de règles de blocage actives |
| `error` | string \| null | Erreur réseau pour cet équipement (`null` si OK) |

### Réponse — client introuvable (`200 OK`)

Ce n'est **pas** une erreur : le code reste `200`.

```json
{
  "phone": "00000000",
  "found": false,
  "crm": null,
  "services_count": null,
  "services": null,
  "recent_invoices": null,
  "fai_count": null,
  "fai": null,
  "errors": {"local": "not_found"}
}
```

---

## 3. Endpoint 2 — Blocage / déblocage (`POST /api/clients/block`)

Bloque ou débloque **un abonnement précis** (identifié par sa MAC) d'un client
sur le routeur (FAI).

### Requête

```
POST /uisp/whatsapp-py/api/clients/block
```

| Élément | Valeur |
|---|---|
| Méthode | `POST` |
| Header `X-Admin-Key` | **obligatoire** — clé d'action |
| Header `Content-Type` | `application/json` |

#### Corps (JSON)

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

> Le `mac` doit appartenir à un abonnement du `phone` fourni (sinon `404`).
> Pour obtenir les MAC d'un client, utilisez d'abord l'endpoint de consultation
> (`fai[].mac`).

#### Exemple cURL — bloquer

```bash
curl -X POST "http://13.49.185.225/uisp/whatsapp-py/api/clients/block" \
  -H "X-Admin-Key: <CLE_ADMIN>" \
  -H "Content-Type: application/json" \
  -d '{"phone":"31400048","mac":"6c:63:f8:b8:cd:0c","action":"block"}'
```

#### Exemple cURL — débloquer

```bash
curl -X POST "http://13.49.185.225/uisp/whatsapp-py/api/clients/block" \
  -H "X-Admin-Key: <CLE_ADMIN>" \
  -H "Content-Type: application/json" \
  -d '{"phone":"31400048","mac":"6c:63:f8:b8:cd:0c","action":"unblock"}'
```

### Réponse (`200 OK`)

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
| `action` | string | Action effectuée |
| `rules_changed` | int | Règles ajoutées (block) / supprimées (unblock). `0` = état déjà conforme |
| `statu_local` | int | Nouveau statut : `2` = bloqué, `0` = actif |
| `local_rows_updated` | int | Lignes mises à jour en base |
| `is_blocked` | bool | État de blocage **vérifié** après l'action |
| `block_rule_count` | int | Règles de blocage actives après l'action |

> **Idempotent** : bloquer un client déjà bloqué (ou débloquer un déjà actif)
> renvoie `200` avec `rules_changed: 0`. Vous pouvez rejouer l'appel sans risque
> de doublon.

---

## 4. Gestion des erreurs

### Codes HTTP

| Code | Signification | Action recommandée |
|---|---|---|
| `200` | Succès | Traiter la réponse |
| `401` | Clé d'API absente ou invalide | Vérifier le header |
| `404` | (block) MAC non rattachée au téléphone | Vérifier le couple phone/mac |
| `422` | Paramètre manquant ou invalide | Corriger la requête |
| `502` | Le routeur n'a pas répondu (block) | Réessayer plus tard |

### Erreurs partielles (consultation)

Sur `/api/clients/lookup`, si une source externe (CRM ou réseau) est
momentanément indisponible, l'API **ne renvoie pas d'erreur HTTP** : elle répond
`200` avec le bloc concerné à `null` et le détail dans `errors`. Exemple :

```json
{
  "found": true,
  "crm": null,
  "fai": [ ... ],
  "errors": {
    "crm": "ReadTimeout: ...",
    "services": "ReadTimeout: ...",
    "invoices": null,
    "fai": null
  }
}
```

➡ **Toujours examiner le bloc `errors`** : un bloc à `null` accompagné d'un
message dans `errors` signifie « donnée temporairement indisponible », pas
« donnée inexistante ». Vous pouvez réessayer plus tard.

---

## 5. Tableaux de référence des statuts

### Statut d'un service (`services[].status`)

| Code | Label |
|---|---|
| 0 | Prepared |
| 1 | Active |
| 2 | Ended |
| 3 | Suspended |
| 4 | Cancelled |
| 5 | Quoted |
| 6 | Inactive |
| 7 | Obsolete |
| 8 | Deferred |

### Statut d'une facture (`recent_invoices[].status`)

| Code | Label |
|---|---|
| 0 | Draft |
| 1 | Unpaid |
| 2 | PartiallyPaid |
| 3 | Paid |
| 4 | Void |
| 5 | ProcessedProforma |

### Statut local d'un équipement (`fai[].statu_local`, `statu_local`)

| Code | Signification |
|---|---|
| 0 | Actif |
| 2 | Bloqué / suspendu |

---

## 6. Recommandations d'intégration

- **Timeout client** : prévoir ≥ 10 secondes (l'API agrège plusieurs sources).
- **Retries** : en cas de `502` ou erreur réseau, réessayer avec un délai (pas de boucle serrée). Les deux endpoints sont sûrs à rejouer (idempotents).
- **Sécurité** : conserver les clés dans un coffre/variable d'environnement, jamais en clair dans le code ni les logs.
- **Note transport** : l'API est actuellement servie en HTTP (non chiffré). À utiliser depuis le réseau interne / VPN tant que HTTPS n'est pas activé. La clé transite en clair sur le réseau.

---

## 7. Récapitulatif rapide

```
# Consultation
GET  /uisp/whatsapp-py/api/clients/lookup?phone=<num>
     Header: X-API-Key: <CLE_LECTURE>

# Blocage / déblocage
POST /uisp/whatsapp-py/api/clients/block
     Header: X-Admin-Key: <CLE_ADMIN>
     Header: Content-Type: application/json
     Body:   {"phone":"<num>","mac":"<mac>","action":"block"|"unblock"}
```

Contact : équipe whatsapp_automation.
