# API Endpoint : Consultation client par téléphone

> Document technique destiné à l'équipe IT / intégration.
> Version 1.0 — 2026-06-10
> Owner : équipe whatsapp_automation

---

## 1. Objectif

Endpoint **HTTP GET en lecture seule** qui, à partir d'un numéro de téléphone
client, agrège en une seule réponse JSON les informations consolidées venant
de trois sources :

1. **Base de données locale (PostgreSQL)** — résolution `téléphone → idclient` (utilisée en interne uniquement, jamais retournée)
2. **CRM UCRM** — identité, statut compte, solde, et **forfait(s) d'abonnement**
3. **Mikrotik (routeur — agit comme FAI)** — statut de blocage réseau

Cas d'usage : support client, dashboards internes, outils de diagnostic,
intégrations tierces.

---

## 2. URL et authentification

### URL publique

```
GET http://13.49.185.225/uisp/whatsapp-py/api/clients/lookup
```

Le service applicatif (FastAPI) tourne sur `127.0.0.1:8010` et n'est **pas** exposé directement à Internet — c'est Apache (reverse proxy sur le port 80) qui route les requêtes vers lui.

### Authentification — Header obligatoire

| Header | Valeur |
|---|---|
| `X-API-Key` | clé partagée (32 octets en hex, 64 caractères) |

La clé est définie côté serveur dans `.env` (variable `CLIENT_API_KEY`) et doit être fournie pour **toute** requête. La clé courante de production sera communiquée séparément (canal sécurisé).

⚠ Sans header `X-API-Key` ou avec une clé invalide → **`401 Unauthorized`** et aucune donnée n'est renvoyée.

---

## 3. Format de la requête

### Méthode
`GET` uniquement (toute autre méthode → `405 Method Not Allowed`)

### Paramètres de query string

| Paramètre | Type | Obligatoire | Description |
|---|---|---|---|
| `phone` | string | **oui** | Numéro de téléphone du client. Accepte tous les formats courants : `37697850`, `22237697850`, `+22237697850`, `22237697850@c.us` |

Le serveur normalise automatiquement le numéro : suppression du préfixe pays `222` et des caractères WhatsApp (`@c.us`, `@s.whatsapp.net`), pour aboutir aux 8 chiffres canoniques.

### Headers
| Header | Obligatoire | Valeur |
|---|---|---|
| `X-API-Key` | **oui** | clé d'API |

### Exemple de requête complète

```http
GET /uisp/whatsapp-py/api/clients/lookup?phone=33848414 HTTP/1.1
Host: 13.49.185.225
X-API-Key: <CLE_API>
Accept: application/json
```

---

## 4. Format de la réponse

`Content-Type: application/json`

### 4.1 Client trouvé (`200 OK`)

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
  "fai_count": 2,
  "fai": [
    {
      "mac": "6c:63:f8:b8:cd:0c",
      "ip": "10.135.4.135/16",
      "statu_local": 2,
      "is_blocked": true,
      "block_rule_count": 1,
      "error": null
    },
    {
      "mac": "d0:21:f9:f0:a1:53",
      "ip": "10.135.7.203/16",
      "statu_local": 0,
      "is_blocked": false,
      "block_rule_count": 0,
      "error": null
    }
  ],
  "errors": {
    "crm": null,
    "services": null,
    "fai": null
  }
}
```

> Note : `fai` est une **liste** — un client peut avoir plusieurs abonnements
> (plusieurs équipements / MAC). L'exemple ci-dessus montre un client à 2
> abonnements ; un client à 1 seul abonnement renvoie une liste à 1 élément.

### 4.2 Description des champs

#### Niveau racine

| Champ | Type | Description |
|---|---|---|
| `phone` | string | Numéro normalisé (8 chiffres) |
| `found` | bool | `true` si le client a été trouvé en DB locale, sinon `false` |
| `crm` | object \| null | Bloc CRM UCRM, `null` si UCRM injoignable ou client introuvable |
| `services_count` | int \| null | Nombre de forfaits/services. `null` si UCRM en échec ou client introuvable |
| `services` | array \| null | Liste détaillée des forfaits. `null` si UCRM en échec |
| `recent_invoices` | array \| null | Les 5 dernières factures (plus récentes d'abord). `null` si UCRM en échec ou client introuvable |
| `fai_count` | int \| null | Nombre d'abonnements/équipements (MAC) du client. `null` si client introuvable |
| `fai` | array \| null | Liste des équipements réseau (un par MAC). `null` si client introuvable |
| `errors` | object | Détail des erreurs par source (voir section 6) |

#### Bloc `crm`

| Champ | Type | Description |
|---|---|---|
| `id` | int | ID interne UCRM |
| `client_type` | int | 1 = résidentiel, 2 = entreprise |
| `first_name` | string \| null | Prénom |
| `last_name` | string \| null | Nom |
| `phone` | string | Téléphone (premier contact UCRM) |
| `balance` | int | Solde dû en MRU (entier) — alias d'`account_outstanding` |
| `registration_date` | string ISO 8601 | Date d'inscription UCRM |
| `is_active` | bool | Le client a-t-il au moins un service actif ? |
| `account_balance` | float | Solde du compte UCRM |
| `account_credit` | float | Crédit pré-payé disponible |
| `account_outstanding` | float | Montant dû non payé |
| `has_suspended_service` | bool | A-t-il un service actuellement suspendu ? |

#### Bloc `services` (liste — un client peut avoir plusieurs forfaits)

| Champ | Type | Description |
|---|---|---|
| `id` | int | ID UCRM du service |
| `name` | string | Nom du forfait (ex : `"AirFiber 15Mb"`) |
| `type` | string | Type UCRM : `"Internet"`, `"IPTV"`, etc. |
| `status` | int | Code UCRM (voir mapping ci-dessous) |
| `status_label` | string | Libellé humain du statut |
| `price` | float | Prix mensuel facturé |
| `currency` | string | Devise (`"MRU"` typiquement) |
| `download_speed_mb` | float | Débit descendant (Mb/s) |
| `upload_speed_mb` | float | Débit montant (Mb/s) |
| `active_from` | string ISO 8601 | Date de début de service |
| `active_to` | string ISO 8601 \| null | Date de fin (null = sans terme) |
| `last_invoiced_date` | string ISO 8601 \| null | Dernière facture émise |
| `prepaid` | bool | Forfait prépayé ? |
| `has_outage` | bool | Panne en cours sur ce service ? |

**Mapping `status` → `status_label`** (codes UCRM officiels) :

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

#### Bloc `recent_invoices` (liste — les 5 dernières factures, plus récentes d'abord)

| Champ | Type | Description |
|---|---|---|
| `id` | int | ID interne UCRM de la facture |
| `number` | string | Numéro de facture (ex : `"024311"`) |
| `created_date` | string ISO 8601 | Date d'émission |
| `due_date` | string ISO 8601 | Date d'échéance |
| `total` | float | Montant total de la facture |
| `amount_paid` | float | Montant déjà payé |
| `amount_to_pay` | float | Reste à payer (`0` = soldée) |
| `currency` | string | Devise (`"MRU"`) |
| `status` | int | Code UCRM (voir mapping ci-dessous) |
| `status_label` | string | Libellé humain du statut |

**Mapping `status` facture → `status_label`** :

| Code | Label |
|---|---|
| 0 | Draft |
| 1 | Unpaid |
| 2 | PartiallyPaid |
| 3 | Paid |
| 4 | Void |
| 5 | ProcessedProforma |

#### Bloc `fai` (liste — un objet par abonnement / équipement)

Un client peut avoir plusieurs abonnements, chacun avec son propre équipement
(MAC) et son propre état réseau. Chaque entrée de la liste contient :

| Champ | Type | Description |
|---|---|---|
| `mac` | string | Adresse MAC de l'équipement de cet abonnement |
| `ip` | string \| null | Adresse IP locale associée (format CIDR, ex : `10.135.4.135/16`) |
| `statu_local` | int | Statut côté base locale : `0` = actif, `2` = suspendu |
| `is_blocked` | bool | `true` si au moins une règle firewall DROP cible la MAC sur le routeur |
| `block_rule_count` | int | Nombre exact de règles DROP actives pour cette MAC |
| `error` | string \| null | Erreur Mikrotik pour cet équipement précis (`null` si OK) |

> Les MAC placeholder (`pending-XXXX`, client sans MAC réel) ou vides ne
> déclenchent pas d'appel routeur : `is_blocked` vaut alors `false`.

#### Bloc `errors`

Object qui contient une clé par source. Valeur `null` si la source a répondu correctement, sinon un message d'erreur court (format `<ExceptionType>: <message>`, tronqué à 200 caractères).

| Clé | Présent quand |
|---|---|
| `local` | Toujours (`"not_found"` si le téléphone n'est pas en DB) |
| `crm` | Présent quand client trouvé en local |
| `services` | Présent quand client trouvé en local |
| `invoices` | Présent quand client trouvé en local |
| `fai` | Présent quand client trouvé en local (1re erreur Mikrotik parmi les équipements, sinon `null`) |

### 4.3 Client introuvable (`200 OK` — pas d'erreur HTTP)

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

C'est un cas **normal**, pas une erreur. Le code HTTP reste `200`.

---

## 5. Codes HTTP

| Code | Quand | Action client recommandée |
|---|---|---|
| **200** | Succès (même si client introuvable ou source partielle) | Examiner `found`, `errors`, et chaque bloc |
| **401** | Header `X-API-Key` absent ou invalide | Vérifier la clé |
| **422** | Paramètre `phone` manquant ou vide | Corriger la requête |
| **405** | Méthode autre que GET | Utiliser GET |
| **502 / 503 / 504** | Apache ne joint pas le service applicatif (rare) | Réessayer après quelques secondes |
| **500** | Bug interne (ne devrait jamais arriver) | Remonter à l'équipe |

⚠ **Important** : un échec ponctuel du CRM ou du Mikrotik ne provoque **pas** de 500. Le serveur retourne 200 avec la (ou les) source en échec à `null` et le détail dans `errors`. Le code appelant doit donc **toujours** examiner `errors` pour décider si la donnée est complète.

---



