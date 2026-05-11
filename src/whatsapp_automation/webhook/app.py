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
