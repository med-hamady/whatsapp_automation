"""Configuration centralisée via variables d'environnement (.env)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


# .env est cherché à la racine du projet (d:\Whatsapp\.env), pas à côté de
# ce fichier — c'est la convention standard et évite la duplication.
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Variable d'environnement manquante : {name}")
    return value or ""


DATABASE_URL = _get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/whatsapp_test")

# UCRM expose en réalité deux APIs distinctes sur le même hôte :
#   - /api/v1.0/...      : billing (paiements) avec auth X-Auth-App-Key
#   - /crm/api/v1.0/...  : CRM/clients (solde) avec auth x-auth-token (UUID)
# UCRM_BASE_URL est la racine hôte (sans le préfixe), commune aux deux.
UCRM_BASE_URL = _get("UCRM_BASE_URL", "http://127.0.0.1:9001")
UCRM_APP_KEY = _get("UCRM_APP_KEY", "fake-app-key")
UCRM_CRM_TOKEN = _get("UCRM_CRM_TOKEN", "fake-crm-token")

# Constantes du compte UCRM client (à fournir en prod via .env).
UCRM_METHOD_ID = _get("UCRM_METHOD_ID", "c081a41c-ed63-49e9-abeb-c099e4297316")
UCRM_USER_ID = int(_get("UCRM_USER_ID", "1639"))
UCRM_CURRENCY = _get("UCRM_CURRENCY", "MRU")

# MIKROTIK_DRIVER pilote le mode d'accès au routeur :
#   - "http"     : tape sur fake_mikrotik (port 9002) — dev/tests.
#   - "routeros" : protocole binaire RouterOS via librouteros — prod.
MIKROTIK_DRIVER = _get("MIKROTIK_DRIVER", "http")

MIKROTIK_BASE_URL = _get("MIKROTIK_BASE_URL", "http://127.0.0.1:9002")
MIKROTIK_HOST = _get("MIKROTIK_HOST", "102.215.95.1")
MIKROTIK_PORT = int(_get("MIKROTIK_PORT", "8728"))
MIKROTIK_USER = _get("MIKROTIK_USER", "Suspension")
MIKROTIK_PASSWORD = _get("MIKROTIK_PASSWORD", "")
MIKROTIK_TIMEOUT = float(_get("MIKROTIK_TIMEOUT", "15"))

# Superviseur réseau (API FAI) : second mécanisme de blocage/déblocage, appliqué
# directement sur le LR du client (SSH) et ré-appliqué toutes les 120 s côté
# superviseur. Coexiste avec le firewall MikroTik : on agit sur les deux.
# Sans URL + clé, le client est désactivé (no-op) — c'est le défaut en dev/tests.
FAI_API_BASE_URL = _get("FAI_API_BASE_URL", "")
FAI_API_KEY = _get("FAI_API_KEY", "")
# Timeout des ACTIONS (unblock) : l'équipe réseau impose >= 60 s, car l'appel
# attend la réponse réelle du LR du client avant de répondre. Un timeout court
# ferait conclure à un échec alors que l'ordre a bien été exécuté.
FAI_API_TIMEOUT = float(_get("FAI_API_TIMEOUT", "90"))
# Timeout de la LECTURE (status) : cet appel ne sollicite pas le LR et il est fait
# en direct dans /api/clients/lookup — on le garde court pour ne pas figer la
# consultation d'une fiche client si le superviseur rame.
FAI_API_STATUS_TIMEOUT = float(_get("FAI_API_STATUS_TIMEOUT", "15"))
# Le superviseur présente un certificat auto-signé : la vérification TLS est
# désactivée POUR CET HÔTE uniquement (la connexion reste chiffrée).
FAI_API_VERIFY_SSL = _get("FAI_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")

ULTRAMSG_BASE_URL = _get("ULTRAMSG_BASE_URL", "http://127.0.0.1:9003")
ULTRAMSG_INSTANCE = _get("ULTRAMSG_INSTANCE", "instance62746")
ULTRAMSG_TOKEN = _get("ULTRAMSG_TOKEN", "fake-token")

# Destinataire WhatsApp qui reçoit les notifications d'échec de paiement
# (client introuvable, OCR raté, CRM injoignable, sur-paiement…). Accepte :
#   - un numéro individuel : "+22248783201"
#   - un ID de groupe :     "120363xxxxxxxxxxxx@g.us"
# Si vide, les notifs sont skippées silencieusement (avec un warning).
SUPPORT_RECIPIENT = _get("SUPPORT_RECIPIENT", "")

AI_OCR_URL = _get("AI_OCR_URL", "http://127.0.0.1:8008")

# Racine projet = parent de src/whatsapp_automation/
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _PROJECT_ROOT / "data"

QUEUE_DB_PATH = _get(
    "QUEUE_DB_PATH",
    str(_DATA_DIR / "queue.db"),
)

# Base SQLite dédiée du dashboard : cache d'événements alimenté depuis les logs
# (les logs restent la source brute, intouchée). Le dashboard lit cette table au
# lieu de parser les logs à chaque requête.
EVENTS_DB_PATH = _get(
    "EVENTS_DB_PATH",
    str(_DATA_DIR / "events.db"),
)

# Dossier où le service ai_ocr archive les samples (images + OCR + label.json)
AI_OCR_DATASET_PATH = _get(
    "AI_OCR_DATASET_PATH",
    str(_DATA_DIR / "dataset"),
)

# Base SQLite dédiée aux paiements reçus d'un numéro non reconnu (client_not_found).
# Préserve les données structurées (montant, txn_id, sample_id...) pour permettre
# un rattachement manuel ultérieur depuis le dashboard, ET sert de mémoire pour
# que les paiements futurs du même numéro soient résolus automatiquement (cf.
# unknown_clients_store.find_client_id_for_phone) — distincte d'events.db
# (simple cache de logs) et de PostgreSQL (schéma clients intouché).
UNKNOWN_CLIENTS_DB_PATH = _get(
    "UNKNOWN_CLIENTS_DB_PATH",
    str(_DATA_DIR / "unknown_clients.db"),
)

N_WORKERS = int(_get("N_WORKERS", "2"))
WORKER_POLL_INTERVAL = float(_get("WORKER_POLL_INTERVAL", "1.0"))

PDF_URL_TEMPLATE = _get(
    "PDF_URL_TEMPLATE",
    "http://127.0.0.1:9003/fake-pdf/{payment_id}",
)

# Clé d'API pour l'endpoint interne /api/clients/lookup (consultation client).
# Vide = endpoint désactivé (toute requête → 401), c'est le comportement par
# défaut tant qu'un opérateur n'a pas explicitement configuré une valeur.
CLIENT_API_KEY = _get("CLIENT_API_KEY", "")

# Clé d'API séparée pour les actions sensibles (/api/clients/block : blocage /
# déblocage d'un client sur le routeur). Distincte de CLIENT_API_KEY pour que la
# clé de lecture seule ne permette pas d'agir sur le réseau. Vide = endpoint
# désactivé (toute requête → 401).
ADMIN_API_KEY = _get("ADMIN_API_KEY", "")

# Tolérance de sous-paiement (MRU). Si (solde CRM - montant payé) > seuil,
# on enregistre le paiement mais on ne débloque pas le client.
# Si ≤ seuil (y compris valeur négative = sur-paiement), on débloque.
UNDERPAYMENT_TOLERANCE = int(_get("UNDERPAYMENT_TOLERANCE", "150"))

# Délai (secondes) au-delà duquel une confirmation dashboard bloquée en
# 'confirming' SANS Job retrouvé en queue est considérée abandonnée (crash
# process, ou exception non gérée, survenu après reserve_for_confirmation()
# mais avant que enqueue() n'ait pu être appelé) et peut être restaurée
# automatiquement vers 'associated' pour permettre un nouvel essai. En
# dessous de ce délai, une confirmation 'confirming' est traitée comme
# potentiellement toujours active (lectures PostgreSQL/UCRM en vol) et n'est
# JAMAIS relâchée par une requête tierce.
UNKNOWN_CLIENT_CONFIRM_TIMEOUT_SECONDS = int(_get("UNKNOWN_CLIENT_CONFIRM_TIMEOUT_SECONDS", "300"))

# Mot de passe d'accès au dashboard de supervision (/dashboard). Lecture seule.
# Vide = dashboard désactivé : la page de login refuse toute connexion et les
# endpoints /dashboard/api/* renvoient 401. À renseigner en prod via .env.
DASHBOARD_PASSWORD = _get("DASHBOARD_PASSWORD", "")
