"""Configuration centralisée via variables d'environnement (.env)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


_ENV_PATH = Path(__file__).resolve().parent / ".env"
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

ULTRAMSG_BASE_URL = _get("ULTRAMSG_BASE_URL", "http://127.0.0.1:9003")
ULTRAMSG_INSTANCE = _get("ULTRAMSG_INSTANCE", "instance62746")
ULTRAMSG_TOKEN = _get("ULTRAMSG_TOKEN", "fake-token")

AI_OCR_URL = _get("AI_OCR_URL", "http://127.0.0.1:8008")

# Racine projet = parent de src/whatsapp_automation/
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _PROJECT_ROOT / "data"

QUEUE_DB_PATH = _get(
    "QUEUE_DB_PATH",
    str(_DATA_DIR / "queue.db"),
)

# Dossier où le service ai_ocr archive les samples (images + OCR + label.json)
AI_OCR_DATASET_PATH = _get(
    "AI_OCR_DATASET_PATH",
    str(_DATA_DIR / "dataset"),
)

N_WORKERS = int(_get("N_WORKERS", "2"))
WORKER_POLL_INTERVAL = float(_get("WORKER_POLL_INTERVAL", "1.0"))

PDF_URL_TEMPLATE = _get(
    "PDF_URL_TEMPLATE",
    "http://127.0.0.1:9003/fake-pdf/{payment_id}",
)

# Tolérance de sous-paiement (MRU). Si (solde CRM - montant payé) > seuil,
# on enregistre le paiement mais on ne débloque pas le client.
# Si ≤ seuil (y compris valeur négative = sur-paiement), on débloque.
UNDERPAYMENT_TOLERANCE = int(_get("UNDERPAYMENT_TOLERANCE", "150"))
