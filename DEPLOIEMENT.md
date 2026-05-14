# Déploiement et lancement — WhatsApp Automation

Guide pas-à-pas pour faire tourner le pipeline sur **l'instance AWS EC2 Windows** de production.

> ⚠ **PostgreSQL** : la base prod (`uisp_client`) existe déjà et est partagée avec d'autres systèmes du SI. Le code Python s'y connecte uniquement en `SELECT` / `INSERT` / `UPDATE` sur des lignes. **Aucun script du repo ne crée, drop ou migre de table**. Ne jamais jouer de `schema.sql` ni d'autre DDL sur cette base.

---

## Étape 1 — RDP sur l'EC2

Connexion Remote Desktop avec les credentials AWS habituels.

---

## Étape 2 — Pré-requis sur la machine

Vérifier qu'ils sont installés :

```powershell
python --version          # >= 3.11
git --version             # version récente
```

Si Python manque : installer depuis [python.org](https://www.python.org/downloads/windows/) en cochant **"Add Python to PATH"**.

---

## Étape 3 — Récupérer le code

```powershell
cd d:\
git clone <repo-url> Whatsapp
cd d:\Whatsapp
```

Si le repo est déjà cloné, juste :

```powershell
cd d:\Whatsapp
git pull
```

---

## Étape 4 — Installer les dépendances Python

```powershell
pip install -r requirements.txt
```

Cela installe entre autres :
- `fastapi`, `uvicorn` — webhook + service IA
- `psycopg[binary]` — driver PostgreSQL
- `librouteros` — RouterOS API pour MikroTik
- `httpx` — appels UCRM / UltraMsg
- `rapidocr`, `onnxruntime`, `pillow` — OCR local

---

## Étape 5 — Créer le fichier `.env`

Le fichier `.env` doit être à la **racine du projet** (`d:\Whatsapp\.env`). Il est gitignored — il ne vient PAS avec `git clone`, il faut le créer à la main.

Copier-coller exactement ce contenu dans `d:\Whatsapp\.env` :

```ini
# ============ PostgreSQL (base prod partagée) ============
# host=localhost car Postgres tourne sur le même EC2. Mot de passe URL-encodé (@ → %40).
DATABASE_URL=postgresql://postgres:Ali%4033848414@localhost:5432/uisp_client

# ============ Service IA local (ai_ocr) ============
AI_OCR_URL=http://127.0.0.1:8008

# ============ UCRM ============
UCRM_BASE_URL=https://13.62.145.152
UCRM_APP_KEY=3tHbf3kiroOJ+aK0A3dbjmrDsSKJoF6t8keOV+lnuyuMOfahAOytrfF1K6a32OEG
UCRM_CRM_TOKEN=d97ed75c-7e6f-40e3-87c0-cb76284dd4e0
UCRM_METHOD_ID=c081a41c-ed63-49e9-abeb-c099e4297316
UCRM_USER_ID=1639
UCRM_CURRENCY=MRU

# ============ MikroTik (RouterOS API) ============
MIKROTIK_DRIVER=routeros
MIKROTIK_HOST=102.215.95.1
MIKROTIK_PORT=8728
MIKROTIK_USER=Suspension
MIKROTIK_PASSWORD=12345
MIKROTIK_TIMEOUT=15

# ============ UltraMsg (passerelle WhatsApp) ============
ULTRAMSG_BASE_URL=https://api.ultramsg.com
ULTRAMSG_INSTANCE=instance62746
ULTRAMSG_TOKEN=9acr79twdsboi8x9

# ============ PDF reçu (proxy PHP servant le PDF généré par UCRM) ============
PDF_URL_TEMPLATE=http://13.49.185.225/uisp/paymentrecue.php?id={payment_id}

# ============ Workers ============
N_WORKERS=2
WORKER_POLL_INTERVAL=1.0
UNDERPAYMENT_TOLERANCE=150
```

---

## Étape 6 — Tester la connectivité (avant tout lancement)

Quatre tests rapides — chacun doit retourner OK avant de continuer.

### 6.1 — PostgreSQL (lecture pure, zéro effet)
```powershell
python -c "import psycopg, os; from dotenv import load_dotenv; load_dotenv(); c=psycopg.connect(os.environ['DATABASE_URL']); cur=c.execute('SELECT COUNT(*) FROM client'); print('clients =', cur.fetchone()[0]); c.close()"
```
→ doit afficher `clients = <nombre>`.

### 6.2 — UCRM (lecture solde d'un client de test)
```powershell
python -c "import asyncio; from whatsapp_automation.worker import ucrm; print(asyncio.run(ucrm.get_balance(1639)))"
```
→ doit retourner un montant (en MRU).

### 6.3 — MikroTik (connexion RouterOS)
```powershell
python -c "from librouteros import connect; from whatsapp_automation import config; api=connect(username=config.MIKROTIK_USER, password=config.MIKROTIK_PASSWORD, host=config.MIKROTIK_HOST, port=config.MIKROTIK_PORT, timeout=config.MIKROTIK_TIMEOUT); print('routeros OK', list(api('/system/identity/print'))); api.close()"
```
→ doit afficher l'identité du routeur.

### 6.4 — UltraMsg (instance status)
```powershell
python -c "import httpx; from whatsapp_automation import config; r=httpx.get(f'{config.ULTRAMSG_BASE_URL}/{config.ULTRAMSG_INSTANCE}/instance/status', params={'token': config.ULTRAMSG_TOKEN}); print(r.status_code, r.json())"
```
→ doit afficher `200` avec un statut `authenticated` ou `connected`.

❌ Si un test échoue → ne pas continuer. Régler le problème (security group, firewall, mauvais token, etc.) avant de lancer les services.

---

## Étape 7 — Lancer les services (lancement manuel)

Le pipeline tourne sur **3 services** dans 3 fenêtres PowerShell distinctes :

```powershell
# Fenêtre 1 — Service IA d'extraction OCR (port 8008)
cd d:\Whatsapp
scripts\run_ai_ocr.bat

# Fenêtre 2 — Webhook qui reçoit les notifications UltraMsg (port 8010)
cd d:\Whatsapp
scripts\run_webhook.bat

# Fenêtre 3 — Worker qui traite la queue
cd d:\Whatsapp
scripts\run_worker.bat
```

Pour avoir N workers en parallèle, ouvrir N fenêtres et lancer `run_worker.bat` dans chacune.

La queue SQLite (`data\queue.db`) est créée automatiquement au premier démarrage.

---

## Étape 8 — Vérifier que ça tourne

### Healthchecks HTTP
```powershell
curl http://127.0.0.1:8008/health     # service IA
curl http://127.0.0.1:8010/health     # webhook
```

### État de la queue
```powershell
python -c "from whatsapp_automation.jobqueue import store; print(store.stats())"
```

### Logs en direct
Les services loggent dans leur fenêtre PowerShell. Pour suivre, garder les fenêtres ouvertes.

---

## Étape 9 — Bascule de l'URL UltraMsg

Une fois les 4 tests de l'étape 6 OK et les services démarrés :

1. Aller dans la console UltraMsg → instance **62746** → Settings → Webhook
2. Remplacer l'URL actuelle (ancien PHP) par : `http://<ip-publique-EC2>:8010/webhook/ultramsg`
3. Vérifier dans Security Group AWS que le port `8010` est ouvert en entrée depuis l'IP UltraMsg
4. Sauvegarder

À partir de ce moment, tous les messages WhatsApp entrants arrivent sur le nouveau pipeline Python.

---

## Étape 10 — Passage en service Windows persistant (NSSM)

Pour que les 3 services redémarrent automatiquement après reboot de l'EC2.

Télécharger [NSSM](https://nssm.cc/download) et le placer dans le PATH, puis :

```powershell
# Service IA OCR
nssm install whatsapp-ai-ocr "d:\Whatsapp\scripts\run_ai_ocr.bat"
nssm set whatsapp-ai-ocr AppDirectory "d:\Whatsapp"
nssm set whatsapp-ai-ocr Start SERVICE_AUTO_START
nssm set whatsapp-ai-ocr AppStdout "d:\Whatsapp\data\logs\ai_ocr.out.log"
nssm set whatsapp-ai-ocr AppStderr "d:\Whatsapp\data\logs\ai_ocr.err.log"

# Webhook
nssm install whatsapp-webhook "d:\Whatsapp\scripts\run_webhook.bat"
nssm set whatsapp-webhook AppDirectory "d:\Whatsapp"
nssm set whatsapp-webhook Start SERVICE_AUTO_START
nssm set whatsapp-webhook DependOnService whatsapp-ai-ocr
nssm set whatsapp-webhook AppStdout "d:\Whatsapp\data\logs\webhook.out.log"
nssm set whatsapp-webhook AppStderr "d:\Whatsapp\data\logs\webhook.err.log"

# Worker (un service par worker souhaité)
nssm install whatsapp-worker-1 "d:\Whatsapp\scripts\run_worker.bat"
nssm set whatsapp-worker-1 AppDirectory "d:\Whatsapp"
nssm set whatsapp-worker-1 Start SERVICE_AUTO_START
nssm set whatsapp-worker-1 DependOnService whatsapp-webhook
nssm set whatsapp-worker-1 AppStdout "d:\Whatsapp\data\logs\worker-1.out.log"
nssm set whatsapp-worker-1 AppStderr "d:\Whatsapp\data\logs\worker-1.err.log"

# Démarrer les 3 services
nssm start whatsapp-ai-ocr
nssm start whatsapp-webhook
nssm start whatsapp-worker-1
```

Vérification :
```powershell
nssm status whatsapp-ai-ocr
nssm status whatsapp-webhook
nssm status whatsapp-worker-1
```

Tous doivent retourner `SERVICE_RUNNING`.

---

## Étape 11 — Surveillance (premières 48 h)

### Suivre les logs en direct
```powershell
Get-Content d:\Whatsapp\data\logs\webhook.out.log -Wait
Get-Content d:\Whatsapp\data\logs\worker-1.out.log -Wait
```

### Vérifier les paiements insérés en DB
```powershell
psql -U postgres -d uisp_client -c "SELECT id_payment, idclient, amount, txn_id FROM paiment ORDER BY id_payment DESC LIMIT 10;"
```

### Vérifier la queue
```powershell
python -c "from whatsapp_automation.jobqueue import store; print(store.stats())"
```
Idéalement `pending=0` et `failed=0` en régime normal.

### Reçus archivés
Les images + OCR de chaque reçu sont stockés dans `data\dataset\store\` pour fine-tuning futur.

---

## Procédure de rollback

Si problème critique pendant la bascule :

1. Console UltraMsg → instance **62746** → remettre l'**ancienne URL** PHP en webhook
2. Stopper les services Python (sans les désinstaller) :
   ```powershell
   nssm stop whatsapp-worker-1
   nssm stop whatsapp-webhook
   nssm stop whatsapp-ai-ocr
   ```
3. Les paiements restants en queue ne sont pas perdus — l'idempotence (`txn_id` dans `processed_payments`) garantit qu'on peut relancer plus tard sans double-traitement.

---

## Règle métier (rappel)

| `balance − paid` | Décision |
|---|---|
| ≤ 0 (paiement exact ou sur-paiement) | ✅ Paiement enregistré + client débloqué |
| 0 < x ≤ 150 MRU (sous-paiement toléré) | ✅ Paiement enregistré + client débloqué |
| > 150 MRU (sous-paiement excessif) | ⚠ Paiement enregistré, client **non débloqué** |

Seuil configurable via `UNDERPAYMENT_TOLERANCE` dans `.env`.
