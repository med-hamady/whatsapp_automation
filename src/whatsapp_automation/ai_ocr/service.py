"""Service FastAPI local : POST /extract → JSON structuré.

Le service écoute uniquement sur 127.0.0.1 (jamais 0.0.0.0). Aucune image
ne quitte la machine ; aucun appel sortant n'est effectué pendant le
traitement.

Concurrence : le pipeline OCR est CPU-bound et synchrone. On le fait tourner
dans un thread pool via `run_in_threadpool`, sinon il bloque l'event loop
d'uvicorn et toutes les requêtes simultanées sont sérialisées.

Sécurité :
- limite stricte de taille (`MAX_UPLOAD_BYTES`) pour bloquer les uploads
  massifs (DoS mémoire).
- whitelist de formats Pillow pour rejeter PDF/SVG/TIFF.
- garde-fou anti decompression-bomb (Image.MAX_IMAGE_PIXELS).
- timeout dur sur l'OCR via asyncio.wait_for.
- détail d'erreur générique côté client, stacktrace seulement en log serveur.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError

from . import __version__
from . import engine as ocr_engine
from .pipeline import process_image
from .dataset.writer import save_sample


# Configure le logging applicatif au niveau INFO, sortie stdout + fichier.
from logging.handlers import RotatingFileHandler as _RFH
_LOG_DIR = os.path.join(os.getcwd(), "data", "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
_file_handler = _RFH(
    os.path.join(_LOG_DIR, "ai_ocr.log"),
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

logger = logging.getLogger("whatsapp_automation.ai_ocr.service")


# Limites configurables via env. Valeurs par défaut adaptées aux captures
# WhatsApp (typiquement 200-800 Ko, jamais au-dessus de 5 Mo).
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(8 * 1024 * 1024)))  # 8 Mo
OCR_TIMEOUT_SECONDS = float(os.environ.get("OCR_TIMEOUT_SECONDS", "60"))
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "BMP"}

# Garde-fou Pillow : refuse une image dont les dimensions décompressées
# dépassent ce nombre de pixels (par défaut 89M). Empêche les "decompression
# bombs" (image 10×10 mais qui décompresse en 50000×50000).
Image.MAX_IMAGE_PIXELS = int(os.environ.get("PIL_MAX_IMAGE_PIXELS", "89_478_485"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    ocr_engine.warmup()
    yield


app = FastAPI(title="ai_ocr", version=__version__, lifespan=lifespan)


@app.get("/health")
def health():
    return {
        "ok": True,
        "model_loaded": ocr_engine.is_ready(),
        "version": __version__,
    }


def _validate_image(image_bytes: bytes) -> None:
    """Vérifie que les bytes sont bien une image dans un format autorisé.
    Lève HTTPException(400) sinon."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            fmt = (im.format or "").upper()
            if fmt not in ALLOWED_FORMATS:
                raise HTTPException(
                    status_code=400,
                    detail=f"unsupported_format: {fmt or 'unknown'}",
                )
            # Force le decode partiel pour détecter les décompression bombs.
            im.verify()
    except HTTPException:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError):
        raise HTTPException(status_code=400, detail="invalid_image")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_image")


def _process(image_bytes: bytes) -> dict:
    """Wrap pipeline.process_image + persistance dataset, exécuté dans un thread."""
    result = process_image(image_bytes)
    prediction = result["prediction"]
    sample_id = save_sample(
        image_bytes=image_bytes,
        ocr_payload=result["ocr_result"].to_dict(),
        prediction=prediction,
    )
    return {"ok": True, **prediction, "sample_id": sample_id}


@app.post("/extract")
async def extract(file: UploadFile = File(...)):
    # Lecture bornée : on stoppe à MAX_UPLOAD_BYTES + 1 et on rejette si dépassé.
    # Cela évite qu'un POST de 1 Go remplisse la RAM avant le check.
    image_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if not image_bytes:
        raise HTTPException(status_code=400, detail="empty_file")
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file_too_large")

    _validate_image(image_bytes)

    try:
        response = await asyncio.wait_for(
            run_in_threadpool(_process, image_bytes),
            timeout=OCR_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("OCR timeout après %.1fs", OCR_TIMEOUT_SECONDS)
        raise HTTPException(status_code=504, detail="ocr_timeout")
    except Exception as exc:
        logger.exception("Pipeline OCR a échoué")
        # On ne renvoie PAS le message d'exception au client (peut leak des
        # chemins absolus, paths Windows, info ONNX, etc.).
        _ = exc
        raise HTTPException(status_code=500, detail="ocr_failed")

    return JSONResponse(response)


def main():
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    workers = int(os.environ.get("UVICORN_WORKERS", "1"))
    uvicorn.run(
        "whatsapp_automation.ai_ocr.service:app",
        host="127.0.0.1",
        port=8008,
        log_level="info",
        workers=workers,
    )


if __name__ == "__main__":
    main()
