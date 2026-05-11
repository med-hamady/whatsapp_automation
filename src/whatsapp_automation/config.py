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

UCRM_BASE_URL = _get("UCRM_BASE_URL", "http://127.0.0.1:9001")
UCRM_APP_KEY = _get("UCRM_APP_KEY", "fake-app-key")

MIKROTIK_BASE_URL = _get("MIKROTIK_BASE_URL", "http://127.0.0.1:9002")

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
