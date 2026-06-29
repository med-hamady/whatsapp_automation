# Migration des services vers Windows Services

> **Date** : 2026-06-10
> **Contexte** : avant cette migration, les 4 services (Apache + 3 services Python)
> tournaient via des `.bat` lancés manuellement dans des fenêtres PowerShell.
> Ce modèle s'est avéré fragile : les services tombaient à chaque fermeture de
> session RDP / fenêtre console, sans redémarrage automatique. Pendant la session
> de mise en place, on a vu les services tomber 3 fois en quelques heures.
>
> Cette doc explique comment ils ont été migrés vers de vrais services Windows
> (Apache en service natif, services Python via **NSSM**), et donne les commandes
> pour les opérer au quotidien.

---

## 1. Architecture cible

| Service | Nom Windows | Mécanisme | Port | Process |
|---|---|---|---|---|
| Apache (reverse proxy) | `Apache2.4` | Natif (`httpd -k install`) | 80 | httpd.exe |
| AI OCR (OCR des reçus) | `whatsapp_ai_ocr` | NSSM | 8008 | python.exe (uvicorn) |
| Worker paiements | `whatsapp_worker` | NSSM | — | python.exe |
| Webhook UltraMsg | `whatsapp_webhook` | NSSM | 8010 | python.exe (uvicorn) |

Tous configurés en **StartType = Automatic** → redémarrage auto au boot serveur.

Routage Apache (déjà en place via `C:\Apache24\conf\extra\httpd-whatsapp-py.conf`) :

```
Internet  →  Apache :80  →  reverse proxy  →  uvicorn 127.0.0.1:8010
```

Paths exposés publiquement :
- `/uisp/whatsapp-py/webhook` → `/webhook` (réception UltraMsg)
- `/uisp/whatsapp-py/health` → `/health`
- `/uisp/whatsapp-py/queue/stats` → `/queue/stats`
- `/uisp/whatsapp-py/api/clients/lookup` → `/api/clients/lookup` (consultation client)

---

## 2. Prérequis

- **NSSM 2.24** installé à `C:\Tools\nssm\nssm.exe`
- **Python** à `C:\Python314\python.exe`
- **Apache** installé à `C:\Apache24\`
- **Projet** à `C:\Users\Administrator\whatsapp_automation\`
- Fichier `.env` à la racine du projet (contient credentials UCRM, Mikrotik, CLIENT_API_KEY, etc.)

### Si NSSM doit être réinstallé

```powershell
$dst = "C:\Tools\nssm"
New-Item -ItemType Directory -Path $dst -Force | Out-Null
Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile "$env:TEMP\nssm-2.24.zip" -UseBasicParsing
Expand-Archive -Path "$env:TEMP\nssm-2.24.zip" -DestinationPath "$env:TEMP\nssm-extract" -Force
Copy-Item "$env:TEMP\nssm-extract\nssm-2.24\win64\nssm.exe" "$dst\nssm.exe" -Force
Unblock-File "$dst\nssm.exe"
```

---

## 3. Commandes opérationnelles (usage quotidien)

### Voir l'état de tous les services

```powershell
Get-Service Apache2.4, whatsapp_*
```

### Redémarrer un service

```powershell
Restart-Service whatsapp_webhook
Restart-Service whatsapp_worker
Restart-Service whatsapp_ai_ocr
Restart-Service Apache2.4
```

### Stop / Start

```powershell
Stop-Service  whatsapp_webhook
Start-Service whatsapp_webhook
```

### Suivre les logs en direct

```powershell
$logs = "C:\Users\Administrator\whatsapp_automation\data\logs"
Get-Content "$logs\webhook-stdout.log" -Wait -Tail 20
Get-Content "$logs\worker-stdout.log"  -Wait -Tail 20
Get-Content "$logs\ai_ocr-stdout.log"  -Wait -Tail 20
Get-Content "C:\Apache24\logs\error.log" -Wait -Tail 20
```

Le webhook a aussi son log applicatif rotatif : `data/logs/webhook.log` (10 MB × 5).

### Vérifier qu'un endpoint répond

```powershell
# Health interne
Invoke-WebRequest "http://127.0.0.1/uisp/whatsapp-py/health" -UseBasicParsing | Select-Object -ExpandProperty Content

# Endpoint consultation client (lecture seule)
$h = @{ "X-API-Key" = (Get-Content C:\Users\Administrator\whatsapp_automation\.env | Select-String "^CLIENT_API_KEY=").ToString().Split("=")[1] }
Invoke-WebRequest "http://127.0.0.1/uisp/whatsapp-py/api/clients/lookup?phone=00000000" -Headers $h -UseBasicParsing | Select-Object -ExpandProperty Content
```

### Vue graphique

```cmd
services.msc
```

---

## 4. Commandes d'installation (référence — déjà exécutées)

### Apache → service natif

```cmd
:: Stop instance manuelle si applicable
taskkill /F /IM httpd.exe

:: Install + auto-start
"C:\Apache24\bin\httpd.exe" -k install
sc config Apache2.4 start= auto
sc start Apache2.4
```

### Services Python via NSSM

Template général — appliqué à chacun des 3 services Python :

```powershell
$nssm = "C:\Tools\nssm\nssm.exe"
$py   = "C:\Python314\python.exe"
$proj = "C:\Users\Administrator\whatsapp_automation"

# ===== AI OCR =====
$svc = "whatsapp_ai_ocr"
& $nssm install $svc $py "-m" "uvicorn" "whatsapp_automation.ai_ocr.service:app" "--host" "127.0.0.1" "--port" "8008" "--log-level" "info"
& $nssm set $svc AppDirectory $proj
& $nssm set $svc AppEnvironmentExtra "PYTHONPATH=$proj\src;$proj"
& $nssm set $svc AppStdout "$proj\data\logs\ai_ocr-stdout.log"
& $nssm set $svc AppStderr "$proj\data\logs\ai_ocr-stderr.log"
& $nssm set $svc Start SERVICE_AUTO_START
& $nssm start $svc

# ===== Worker paiements =====
$svc = "whatsapp_worker"
& $nssm install $svc $py "-m" "whatsapp_automation.worker.main"
& $nssm set $svc AppDirectory $proj
& $nssm set $svc AppEnvironmentExtra "PYTHONPATH=$proj\src;$proj"
& $nssm set $svc AppStdout "$proj\data\logs\worker-stdout.log"
& $nssm set $svc AppStderr "$proj\data\logs\worker-stderr.log"
& $nssm set $svc Start SERVICE_AUTO_START
& $nssm start $svc

# ===== Webhook (uvicorn) =====
$svc = "whatsapp_webhook"
& $nssm install $svc $py "-m" "uvicorn" "whatsapp_automation.webhook.app:app" "--host" "127.0.0.1" "--port" "8010" "--log-level" "info"
& $nssm set $svc AppDirectory $proj
& $nssm set $svc AppEnvironmentExtra "PYTHONPATH=$proj\src;$proj"
& $nssm set $svc AppStdout "$proj\data\logs\webhook-stdout.log"
& $nssm set $svc AppStderr "$proj\data\logs\webhook-stderr.log"
& $nssm set $svc Start SERVICE_AUTO_START
& $nssm start $svc
```

**Note sur les paramètres** :
- `AppDirectory` = répertoire courant du process (où vit `.env`) — équivalent du `cd /d` des `.bat`
- `AppEnvironmentExtra` = variables d'env additionnelles — équivalent du `set PYTHONPATH=...` des `.bat`
- `AppStdout` / `AppStderr` = redirection des sorties (NSSM les capture, sinon perdues)
- `Start SERVICE_AUTO_START` = démarrage auto au boot serveur
- Utilisateur d'exécution : **LocalSystem** par défaut (suffisant pour ce projet)

---

## 5. Rollback (retour aux .bat manuels)

```powershell
# Désinstaller les services Python (NSSM)
foreach ($svc in @("whatsapp_webhook", "whatsapp_worker", "whatsapp_ai_ocr")) {
    Stop-Service $svc -ErrorAction SilentlyContinue
    & "C:\Tools\nssm\nssm.exe" remove $svc confirm
}

# Désinstaller Apache service
Stop-Service Apache2.4
& "C:\Apache24\bin\httpd.exe" -k uninstall
```

Puis relancer les `.bat` comme avant via `scripts\run_*.bat`.

---

## 6. Troubleshooting

### Un service ne démarre pas

```powershell
# 1. Voir le statut détaillé
Get-Service whatsapp_webhook | Format-List *

# 2. Voir les derniers logs stderr (souvent l'erreur Python est là)
Get-Content C:\Users\Administrator\whatsapp_automation\data\logs\webhook-stderr.log -Tail 30

# 3. Tester manuellement la commande exacte (sans NSSM)
$env:PYTHONPATH = "C:\Users\Administrator\whatsapp_automation\src;C:\Users\Administrator\whatsapp_automation"
cd C:\Users\Administrator\whatsapp_automation
C:\Python314\python.exe -m uvicorn whatsapp_automation.webhook.app:app --host 127.0.0.1 --port 8010 --log-level info
```

### Apache : 503 sur les routes proxy

Le service uvicorn cible (port 8010) ne répond pas. Vérifier :
```powershell
Get-Service whatsapp_webhook
Get-NetTCPConnection -State Listen -LocalPort 8010
```
Si downloaded mais service Running : un crash silencieux — regarder `webhook-stderr.log`.

### Modifier la config d'un service NSSM

```powershell
# Modifier un paramètre (ex: les arguments)
& "C:\Tools\nssm\nssm.exe" set whatsapp_webhook AppParameters "-m" "uvicorn" "..." "--port" "8011"
Restart-Service whatsapp_webhook

# Voir la config actuelle d'un service
& "C:\Tools\nssm\nssm.exe" dump whatsapp_webhook
```

### Crash boucle d'un service

NSSM est configuré par défaut pour **restart automatique** sur crash. Si le service crash sans cesse, regarder `*-stderr.log` pour la cause racine, puis :
```powershell
Stop-Service whatsapp_webhook
# Corriger le problème (code, .env, etc.)
Start-Service whatsapp_webhook
```

---

## 7. Bénéfices obtenus

- ✅ Survie au logout RDP / fermeture de session
- ✅ Redémarrage automatique au boot serveur
- ✅ Restart propre via `Restart-Service`
- ✅ Auto-restart sur crash (NSSM par défaut)
- ✅ Logs centralisés dans `data/logs/*-stdout.log` et `*-stderr.log`
- ✅ Plus de fenêtres console à garder ouvertes
- ✅ Gestion graphique via `services.msc`
