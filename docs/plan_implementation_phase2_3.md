# Plan d'implémentation — Phases 2 & 3 (webhook + worker Python)

## Contexte

Phase 1 (module IA local [ai_ocr/](../ai_ocr/)) est terminée. On entame maintenant la migration de [webhook.php](../webhook.php) et [remove_suspende_whatsapp.php](../remove_suspende_whatsapp.php) vers Python, avec une **séparation stricte des responsabilités** :

- **Webhook** : reçoit le message WhatsApp, prépare TOUT (OCR, lookup, validations) et empile un job complet en queue. Répond à UltraMsg en < 100 ms.
- **Worker** : reçoit un job 100 % prêt à consommer, exécute la logique métier (UCRM, MikroTik, PDF). Aucun lookup, aucune validation.

Volumes attendus : quelques centaines de paiements/jour, pics à ~20 simultanés. SQLite suffit comme queue (pas besoin de Redis).

---

## Architecture finale

```
UltraMsg ──POST──▶ webhook (FastAPI)
                     │
                     ├─ download image
                     ├─ POST 127.0.0.1:8008/extract (ai_ocr)
                     ├─ DB lookup client (phone → id, MAC, statut)
                     ├─ Validations (idempotence txn_id, statut, montant)
                     ├─ INSERT job complet en SQLite queue
                     └─ HTTP 200 OK (< 100 ms)

                              │
                              ▼ (queue SQLite, table jobs)

  worker process ×N (poll → claim → exécute)
                     │
                     ├─ POST UCRM /payments → paymentId
                     ├─ INSERT paiement DB locale
                     ├─ Connexion MikroTik + suppression règle firewall (par MAC)
                     ├─ UPDATE statut client "actif"
                     └─ Fetch PDF + envoi UltraMsg /messages/document
```

---

## Structure des fichiers à créer

```
d:\Whatsapp\
└── whatsapp_py\                       ← nouveau module
    ├── README.md
    ├── requirements.txt
    ├── config.py                      ← lecture env vars (UCRM key, MikroTik creds, etc.)
    │
    ├── webhook\
    │   ├── app.py                     ← FastAPI app + endpoint /webhook
    │   ├── pipeline.py                ← orchestration : OCR → lookup → validate → enqueue
    │   ├── ai_ocr_client.py           ← appel HTTP au service ai_ocr local
    │   ├── client_lookup.py           ← phone → {id, MAC, status} via PostgreSQL
    │   ├── validators.py              ← idempotence, statut, montant > 0
    │   ├── phone.py                   ← parse +222, retrait @c.us, etc.
    │   └── ultramsg_payload.py        ← parse JSON UltraMsg entrant
    │
    ├── worker\
    │   ├── main.py                    ← boucle de polling + dispatch
    │   ├── handlers.py                ← orchestration : UCRM → DB → MikroTik → PDF
    │   ├── ucrm.py                    ← client UCRM (création paiement, get amount)
    │   ├── mikrotik.py                ← client RouterOS API (unblock par MAC)
    │   ├── ultramsg_send.py           ← envoi document via UltraMsg
    │   └── pdf_fetch.py               ← récupère le PDF généré côté PHP existant
    │
    ├── queue\
    │   ├── schema.sql                 ← tables jobs + processed_payments
    │   └── store.py                   ← enqueue, claim_next, mark_done, mark_failed
    │
    ├── db\
    │   ├── postgres.py                ← pool psycopg2/asyncpg vers DB locale existante
    │   └── queries.py                 ← GetClientByPhoneNumber, insert_paiement, etc.
    │
    ├── models\
    │   └── job.py                     ← pydantic : Job, Client, Payment, Source
    │
    ├── tests\
    │   ├── test_validators.py
    │   ├── test_queue.py
    │   ├── test_webhook_pipeline.py   ← mock ai_ocr + DB
    │   └── test_handlers.py           ← mock UCRM + MikroTik + UltraMsg
    │
    └── scripts\
        ├── run_webhook.bat            ← uvicorn whatsapp_py.webhook.app:app
        ├── run_worker.bat             ← python -m whatsapp_py.worker.main
        └── init_db.py                 ← crée le fichier queue.db avec le schema
```

`webhook.php` et `remove_suspende_whatsapp.php` **restent intacts** pendant la migration pour permettre une bascule progressive.

---

## Modèle du Job (contrat webhook ↔ worker)

```python
# whatsapp_py/models/job.py
class Client(BaseModel):
    id: int
    phone: str               # ex "37697850" sans indicatif
    mac_address: str         # ex "AA:BB:CC:DD:EE:FF"
    current_status: str      # "suspended" | "actif"

class Payment(BaseModel):
    amount_mru: int
    txn_id: str
    date_heure: Optional[str]  # ISO 8601
    operator: str              # "bankily" | "sedad" | "masrvi" | ...

class Source(BaseModel):
    wnum: str                # numéro WhatsApp d'envoi (peut différer si paie pour autre)
    sample_id: str           # référence dataset ai_ocr
    received_at: str         # ISO 8601 UTC

class Job(BaseModel):
    job_id: str              # uuid
    client: Client
    payment: Payment
    source: Source
```

Le worker reçoit ce payload **complet** et n'a jamais à interroger la DB pour récupérer infos client/paiement.

---

## Phase 2 — Webhook Python

### 2.1 Endpoint

`POST /webhook` (URL à pointer depuis UltraMsg) — accepte JSON UltraMsg, répond immédiatement 200.

```python
@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.json()
    asyncio.create_task(pipeline.process(payload))   # détaché
    return {"ok": True}
```

→ La réponse 200 part en < 100 ms. Le pipeline tourne en arrière-plan.

### 2.2 Pipeline (orchestration côté webhook)

```python
async def process(payload):
    parsed = parse_ultramsg(payload)
    if not parsed.media_url:
        return                                       # ignore les textes seuls

    image_bytes = await download_image(parsed.media_url, max_bytes=8_000_000)

    extraction = await ai_ocr_extract(image_bytes)   # POST 127.0.0.1:8008/extract
    if extraction.montant is None:
        log.info("no amount extracted, drop")
        return

    if extraction.txn_id and await db.payment_processed(extraction.txn_id):
        log.info("idempotence: txn_id déjà traité")
        return

    client = (await db.get_client_by_phone(parsed.from_phone)
              or await db.get_client_by_phone(parsed.body_phone))
    if client is None:
        log.warning("no client matched")
        return

    if client.status != "suspended":
        log.info("client pas suspendu, skip")
        return

    if extraction.montant <= 0:
        log.warning("invalid amount")
        return

    job = Job(
        job_id=uuid4().hex,
        client=Client.from_db(client),
        payment=Payment(
            amount_mru=extraction.montant,
            txn_id=extraction.txn_id,
            date_heure=extraction.date_heure,
            operator=extraction.template,
        ),
        source=Source(
            wnum=parsed.from_phone,
            sample_id=extraction.sample_id,
            received_at=utc_now_iso(),
        ),
    )
    await queue.enqueue(job)
```

### 2.3 Idempotence (centrale)

Table `processed_payments` (txn_id UNIQUE) consultée AVANT l'enqueue. Si UltraMsg renvoie 3 fois la même image (rejeu), seul le premier passe.

### 2.4 Réutilise

- [ai_ocr/](../ai_ocr/) (service existant) — appel HTTP `127.0.0.1:8008/extract`.
- DB PostgreSQL existante (mêmes tables que `connect.php` / `admin.php`).
- Aucune duplication de schéma.

---

## Phase 3 — Worker Python

### 3.1 Boucle principale

```python
# whatsapp_py/worker/main.py
async def run_forever():
    while True:
        job = await queue.claim_next(worker_id=WORKER_ID)
        if job is None:
            await asyncio.sleep(1)
            continue
        try:
            await handlers.process(job)
            await queue.mark_done(job.id)
        except RetryableError as e:
            await queue.mark_retry(job.id, str(e))
        except Exception as e:
            await queue.mark_failed(job.id, str(e))
            log.exception("job %s failed", job.id)
```

`claim_next` utilise une transaction SQLite atomique (`BEGIN IMMEDIATE` + `UPDATE ... WHERE status='pending' LIMIT 1`) pour permettre N workers concurrents sans double-traitement.

### 3.2 Handler (logique métier)

```python
async def process(job: Job):
    # 1. Vérif montant CRM (sanity, non bloquant)
    crm_amount = await ucrm.get_client_balance(job.client.id)

    # 2. Création paiement UCRM
    payment_id = await ucrm.create_payment(
        client_id=job.client.id,
        amount=job.payment.amount_mru,
        note=f"WhatsApp {job.payment.operator} txn={job.payment.txn_id}",
    )

    # 3. Insertion paiement DB locale
    await db.insert_paiement(
        id_client=job.client.id,
        amount=job.payment.amount_mru,
        ucrm_payment_id=payment_id,
        txn_id=job.payment.txn_id,
    )

    # 4. Marquer txn_id traité (idempotence définitive)
    await db.mark_processed(job.payment.txn_id, payment_id)

    # 5. Déblocage MikroTik
    async with mikrotik.connect() as router:
        rule_id = await router.find_filter_by_mac(job.client.mac_address)
        if rule_id is not None:
            await router.remove_filter(rule_id)

    # 6. Statut client "actif"
    await db.update_client_status(job.client.id, "actif")

    # 7. Récupération + envoi du PDF
    pdf_url = f"http://13.49.185.225/uisp/paymentrecue.php?id={payment_id}"
    await ultramsg.send_document(
        to=f"+222{job.client.phone}",
        document_url=pdf_url,
        filename=f"recu_{payment_id}.pdf",
        caption="Votre paiement a été reçu. Merci !",
    )
```

### 3.3 Retry / erreurs

Chaque étape est classée :

| Étape | Type d'erreur | Action |
|---|---|---|
| UCRM create_payment | 5xx, timeout | retry (5 fois, backoff exponentiel) |
| UCRM create_payment | 4xx | failed (abandon, alerte humain) |
| DB insert | erreur SQL | retry 3 fois |
| MikroTik connect | timeout | retry 3 fois |
| MikroTik remove_filter | rule introuvable | log warning, **continue** (client peut-être déjà débloqué) |
| UltraMsg send_document | 5xx | retry 5 fois |
| UltraMsg send_document | 4xx | failed (mais paiement déjà OK côté CRM, alerte humain) |

Politique : **ne jamais répéter une opération avec effet de bord déjà réalisée**. Si UCRM a créé le paiement et que MikroTik plante, on ne recrée pas le paiement au retry — on continue à partir de l'étape suivante. Pour ça, on persiste l'avancement (`step_done`) dans la table `jobs`.

### 3.4 Concurrence

`N_WORKERS` configurable (env var). Default = 4. Chaque worker tire un job à la fois ; SQLite gère le verrouillage. Pour scaler : un seul fichier `queue.db` + N processus Python.

---

## Schéma SQLite (queue)

```sql
-- whatsapp_py/queue/schema.sql

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT UNIQUE NOT NULL,
    txn_id          TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',     -- pending | processing | done | retry | failed
    step_done       TEXT,                                 -- dernière étape réussie (pour reprise)
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 5,
    next_attempt_at REAL NOT NULL,
    last_error      TEXT,
    worker_id       TEXT,
    created_at      REAL NOT NULL,
    started_at      REAL,
    finished_at     REAL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_next ON jobs(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS processed_payments (
    txn_id          TEXT PRIMARY KEY,
    ucrm_payment_id TEXT,
    job_id          TEXT,
    processed_at    REAL NOT NULL
);
```

`txn_id` est la clé d'idempotence. Toute la chaîne (webhook + worker) consulte/écrit ces deux tables.

---

## Secrets / configuration

Fichier `.env` (non commité, exclu du repo) chargé par `config.py` :

```
DATABASE_URL=postgresql://user:pass@localhost/uisp
UCRM_BASE_URL=https://13.62.145.152/api/v1.0
UCRM_APP_KEY=...
MIKROTIK_HOST=102.215.95.1
MIKROTIK_USER=Suspension
MIKROTIK_PASSWORD=...
ULTRAMSG_INSTANCE=instance62746
ULTRAMSG_TOKEN=1i4743y9djegkgb6
AI_OCR_URL=http://127.0.0.1:8008
QUEUE_DB_PATH=d:/Whatsapp/whatsapp_py/queue.db
N_WORKERS=4
```

→ Premier pas vers la sortie des secrets hardcodés actuellement dans le code PHP (problème mentionné dans [CLAUDE.md §7](../CLAUDE.md)).

---

## Dépendances Python à ajouter

```
fastapi
uvicorn[standard]
httpx                         # appels HTTP async (UCRM, UltraMsg, ai_ocr)
psycopg2-binary               # PostgreSQL (ou asyncpg si on veut tout async)
librouteros                   # MikroTik RouterOS API
pydantic
python-dotenv                 # lecture .env
pytest
pytest-asyncio
respx                         # mock httpx pour les tests
```

---

## Migration par étapes (cutover)

### Étape A — Déploiement parallèle (0 risque)
1. Service `ai_ocr` reste démarré (port 8008).
2. Démarrer `whatsapp_py.webhook` sur **port 8010** (différent de PHP).
3. Démarrer `whatsapp_py.worker` (N=2).
4. Le webhook PHP continue à recevoir UltraMsg en production.
5. Mode "shadow" : on copie chaque webhook entrant aussi vers Python pour comparer les sorties sans agir.

### Étape B — Bascule progressive
6. Configurer UltraMsg pour pointer sur le webhook Python (port 8010).
7. Surveillance pendant 24-48 h via logs structurés.
8. Si problème : rebasculer sur PHP en une ligne de config UltraMsg.

### Étape C — Décommissionnement PHP
9. Une fois stabilisé (1 semaine), archive `webhook.php` et `remove_suspende_whatsapp.php`.
10. Garde la DB et `paymentrecue.php` (génération PDF) tels quels.

---

## Tests

### Tests unitaires
- `test_validators.py` : idempotence, statut, montant
- `test_queue.py` : enqueue, claim atomique (multi-thread), retry, mark_done
- `test_phone.py` : parsing +222, @c.us
- `test_webhook_pipeline.py` : mock ai_ocr + DB → vérifier qu'un job est bien construit
- `test_handlers.py` : mock UCRM + MikroTik + UltraMsg → vérifier ordre + idempotence

### Tests d'intégration (en environnement de staging)
- Envoyer une vraie image au webhook → vérifier que le job apparaît en queue, est consommé, et que le client est bien débloqué.
- Test de rejeu : poster 3 fois la même image → 1 seul paiement créé.
- Test de panne MikroTik : couper le réseau → vérifier que le job passe en retry.
- Test de charge : 50 webhooks en parallèle → vérifier latence < 500 ms et 0 double paiement.

---

## Fichiers critiques à lire avant implémentation

- [webhook.php](../webhook.php) — logique actuelle (parsing UltraMsg, OCR, lookup, appel HTTP).
- [remove_suspende_whatsapp.php](../remove_suspende_whatsapp.php) — toute la logique métier à porter.
- [crm_id_Client.php](../crm_id_Client.php) — pattern API UCRM (`X-Auth-App-Key`).
- `../admin.php` — méthodes DB : `GetClientByPhoneNumber`, `insert_paiement`, `GetClientIdRouter`, `EditStatuClientID`.
- `../testpayment.php` — `UcrmApiAccess_pay::setPayment()` (création paiement UCRM).
- `../ROUTEROS_API.php` — protocole MikroTik (à porter via `librouteros`).
- [.claude/AUDIT_webhook.md](../.claude/AUDIT_webhook.md) — 27 problèmes identifiés à éviter.

---

## Vérification end-to-end

1. **Initialiser la queue** : `python -m whatsapp_py.scripts.init_db` → crée `queue.db`.
2. **Lancer les 3 services** :
   ```
   ai_ocr\scripts\run_service.bat            # port 8008
   whatsapp_py\scripts\run_webhook.bat       # port 8010
   whatsapp_py\scripts\run_worker.bat        # N workers
   ```
3. **Simuler un webhook UltraMsg** :
   ```
   curl -X POST http://127.0.0.1:8010/webhook \
        -H "Content-Type: application/json" \
        -d @tests/fixtures/ultramsg_payload.json
   ```
4. **Vérifier en DB** :
   - table `jobs` : job créé avec status `pending`, puis `processing`, puis `done`.
   - table `processed_payments` : `txn_id` inséré.
   - DB UISP : paiement inséré, statut client à "actif".
5. **Vérifier côté MikroTik** : règle firewall du client supprimée.
6. **Vérifier WhatsApp** : le client reçoit le PDF.
7. **Test de charge** : `wrk -t10 -c50 -d30s --script post.lua http://127.0.0.1:8010/webhook` → latence p99 < 200 ms.
8. **Test idempotence** : renvoyer le même payload 3× → 1 seul paiement créé.

---

## Hors-scope (volontairement)

- Pas de réécriture de la génération de PDF (`paymentrecue.php`) — on continue à appeler l'URL PHP existante.
- Pas de migration de la DB PostgreSQL — on utilise les mêmes tables.
- Pas de Docker dans cette phase (peut venir plus tard).
- Pas de monitoring/alerting complet — juste des logs structurés + un endpoint `/health`. Prometheus/Grafana en phase ultérieure.
- Pas de fine-tuning du modèle IA — c'est un autre chantier (déjà câblé via dataset/store).

---

## Estimation

| Phase | Tâche | Estimation |
|---|---|---|
| 2 | Webhook FastAPI + pipeline + idempotence + tests | 2-3 j |
| 3 | Worker + handlers (UCRM, MikroTik, UltraMsg) + retry + tests | 3-4 j |
| 3 | Queue SQLite + concurrence + reprise sur incident | 1 j |
| — | Tests d'intégration sur staging | 1 j |
| — | Cutover progressif + monitoring | 1-2 j |
| **Total** | | **8-11 j-homme** |
