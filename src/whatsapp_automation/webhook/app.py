"""FastAPI app du webhook. Endpoint :
   POST /webhook  — reçoit UltraMsg, répond 200 OK en < 100 ms.

La logique métier est offload-ée dans une tâche asyncio détachée pour ne
pas bloquer la réponse à UltraMsg (qui sinon timeout et rejoue le message).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from .. import __version__
from ..jobqueue import store as queue_store
from . import pipeline


# Configure le logging applicatif au niveau INFO, sortie stdout + fichier.
# uvicorn --log-level info ne configure QUE ses propres loggers ; sans cette
# ligne, les `logger.info(...)` de l'app sont muets (root = WARNING).
# `force=True` pour réécraser une éventuelle config posée par uvicorn.
import os as _os
from logging.handlers import RotatingFileHandler as _RFH
_LOG_DIR = _os.path.join(_os.getcwd(), "data", "logs")
_os.makedirs(_LOG_DIR, exist_ok=True)
_fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
_file_handler = _RFH(
    _os.path.join(_LOG_DIR, "webhook.log"),
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setFormatter(_fmt)
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_fmt)
logging.basicConfig(
    level=logging.INFO,
    handlers=[_file_handler, _stream_handler],
    force=True,
)

logger = logging.getLogger("whatsapp_automation.webhook")


@asynccontextmanager
async def lifespan(app: FastAPI):
    queue_store.init_db()
    yield


app = FastAPI(title="whatsapp_automation.webhook", version=__version__, lifespan=lifespan)


@app.get("/health")
def health():
    return {"ok": True, "service": "webhook", "version": __version__, "queue": queue_store.stats()}


@app.post("/webhook")
async def webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "reason": "invalid_json"}

    # Détache le traitement — UltraMsg reçoit 200 immédiatement.
    asyncio.create_task(_safe_process(payload))
    return {"ok": True}


async def _safe_process(payload: dict):
    try:
        result = await pipeline.process(payload)
        logger.info("pipeline result: %s", result)
    except Exception:
        logger.exception("pipeline crashed")


@app.post("/webhook/sync")
async def webhook_sync(request: Request):
    """Variante SYNCHRONE pour tests : on attend la fin du pipeline et on
    renvoie son résumé. NE PAS pointer UltraMsg dessus en prod."""
    payload = await request.json()
    result = await pipeline.process(payload)
    return result


@app.get("/queue/stats")
def queue_stats():
    return queue_store.stats()
