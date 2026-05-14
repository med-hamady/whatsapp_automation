# Configuration Webhook UltraMsg → Python (Apache reverse proxy)

## Objectif

Faire en sorte qu'UltraMsg envoie ses POST WhatsApp vers le nouveau webhook Python (`whatsapp_automation`) **sans casser** l'ancien webhook PHP qui continue de tourner en parallèle.

## Architecture

```
Internet (UltraMsg)
        │ POST
        ▼
http://13.49.185.225:80  (Apache, port 80 déjà ouvert sur AWS SG)
        │
        ├─ /uisp/Whatsapp/webhook.php      ──► ANCIEN (PHP, inchangé, prod)
        │
        └─ /uisp/whatsapp-py/webhook       ──► NOUVEAU (reverse proxy)
                                                    │
                                                    ▼
                                            127.0.0.1:8010  (FastAPI Python, localhost only)
```

Le webhook Python n'est **pas exposé directement à Internet** — il reste bindé sur `127.0.0.1`. Apache fait le pont. Plus sûr : si Uvicorn plante ou expose un endpoint sensible, ce n'est pas joignable depuis l'extérieur.

## URLs publiques

| URL publique | Cible interne | Usage |
|---|---|---|
| `POST http://13.49.185.225/uisp/whatsapp-py/webhook` | `127.0.0.1:8010/webhook` | À configurer dans le dashboard UltraMsg |
| `GET  http://13.49.185.225/uisp/whatsapp-py/health` | `127.0.0.1:8010/health` | Monitoring / healthcheck |
| `GET  http://13.49.185.225/uisp/whatsapp-py/queue/stats` | `127.0.0.1:8010/queue/stats` | État de la queue SQLite |

L'ancien `http://13.49.185.225/uisp/Whatsapp/webhook.php` continue de fonctionner comme avant.

## Modifications effectuées

### 1. Activation des modules proxy Apache

Fichier : `C:\Apache24\conf\httpd.conf` (lignes ~143 et ~152) — décommenter :

```apache
LoadModule proxy_module modules/mod_proxy.so
LoadModule proxy_http_module modules/mod_proxy_http.so
```

### 2. Nouveau fichier de conf reverse proxy

Fichier : `C:\Apache24\conf\extra\httpd-whatsapp-py.conf` — à créer :

```apache
<IfModule proxy_module>
    ProxyRequests Off
    ProxyPreserveHost On

    ProxyPass        /uisp/whatsapp-py/webhook  http://127.0.0.1:8010/webhook
    ProxyPassReverse /uisp/whatsapp-py/webhook  http://127.0.0.1:8010/webhook

    ProxyPass        /uisp/whatsapp-py/health       http://127.0.0.1:8010/health
    ProxyPassReverse /uisp/whatsapp-py/health       http://127.0.0.1:8010/health
    ProxyPass        /uisp/whatsapp-py/queue/stats  http://127.0.0.1:8010/queue/stats
    ProxyPassReverse /uisp/whatsapp-py/queue/stats  http://127.0.0.1:8010/queue/stats

    <Location /uisp/whatsapp-py/>
        Require all granted
    </Location>
</IfModule>
```

### 3. Inclusion dans `httpd.conf`

Fichier : `C:\Apache24\conf\httpd.conf` (ligne ~531) — ajouté juste après `proxy-html.conf` :

```apache
Include conf/extra/httpd-whatsapp-py.conf
```

### 4. Redémarrage Apache

Apache tourne en processus **standalone** (pas en service Windows), donc :

1. Identifier les PIDs `httpd.exe` (parent + worker) — ex : 872 et 12464.
2. Stopper ces PIDs : `Stop-Process -Id 872,12464 -Force`.
3. Relancer :

```powershell
Start-Process httpd.exe -WindowStyle Hidden -WorkingDirectory C:\Apache24\bin
```

Vérifier que deux nouveaux PIDs (parent + worker) sont actifs.

## Lancer le webhook Python

```bat
scripts\run_webhook.bat
```

Ce script lance Uvicorn sur `127.0.0.1:8010` avec `PYTHONPATH` correctement positionné.

## Tests de validation

| Test | Commande | Résultat attendu |
|---|---|---|
| Syntaxe config Apache | `httpd -t` | `Syntax OK` |
| Python direct | `curl http://127.0.0.1:8010/health` | `200` |
| Via Apache (local) | `curl http://127.0.0.1/uisp/whatsapp-py/health` | `200` |
| POST via Apache (local) | `curl -X POST http://127.0.0.1/uisp/whatsapp-py/webhook -H "Content-Type: application/json" -d '{}'` | `200 {"ok":true}` |
| Depuis l'extérieur | `curl http://13.49.185.225/uisp/whatsapp-py/health` | `200` |

## Dépannage

- **`httpd -t` échoue** → vérifier l'ordre dans `httpd.conf` : `LoadModule proxy_*` doivent être décommentés **avant** le `Include conf/extra/httpd-whatsapp-py.conf`.
- **502 Bad Gateway via Apache** → le webhook Python n'écoute pas. Relancer `scripts\run_webhook.bat` et vérifier `netstat -ano | findstr :8010`.
- **Connexion refusée depuis l'extérieur** → vérifier l'AWS Security Group : port 80 doit être ouvert (déjà le cas en prod).
- **Apache ne redémarre pas via service** → c'est normal, il tourne en standalone ; utiliser la procédure Stop-Process + Start-Process ci-dessus.
