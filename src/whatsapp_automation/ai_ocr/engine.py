"""Wrapper OCR singleton (RapidOCR / ONNXRuntime).

RapidOCR utilise les mêmes modèles que PaddleOCR exportés en ONNX. Avantages
par rapport à PaddleOCR : ~50 Mo au lieu de 1 Go, pas de dépendance
PaddlePaddle, compatible Python 3.13. Qualité de reconnaissance équivalente.

Concurrence : par défaut ONNX Runtime alloue tous les vCPUs disponibles à
chaque session (intra_op_num_threads=-1). Quand plusieurs requêtes arrivent
en parallèle elles se battent pour le même CPU. Les invocations de
``self._ocr(img)`` sont sérialisées par ``_run_lock`` car la session ONNX
sous-jacente n'est pas garantie thread-safe pour tous les builds RapidOCR.
"""

from __future__ import annotations

import gc
import io
import logging
import os
from dataclasses import dataclass, field
from threading import Lock
from typing import List, Optional

from PIL import Image, ImageOps
import numpy as np


logger = logging.getLogger(__name__)


@dataclass
class OcrBox:
    text: str
    confidence: float
    bbox: List[List[float]] = field(default_factory=list)


@dataclass
class OcrResult:
    boxes: List[OcrBox]
    text: str

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "boxes": [
                {"text": b.text, "confidence": b.confidence, "bbox": b.bbox}
                for b in self.boxes
            ],
        }


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, "true" if default else "false").lower() in ("1", "true", "yes")


_DEFAULT_MAX_WIDTH = _env_int("OCR_MAX_WIDTH", 1500)
# Cap sur la *plus grande* dimension (largeur OU hauteur). Sans ça, un
# screenshot WhatsApp vertical type 1080×4500 passe tel quel au détecteur
# DBNet (entrée dynamique) et fait exploser l'allocation ONNX en
# concurrence — "bad allocation" sur les nœuds Clip/Add du détecteur.
_DEFAULT_MAX_DIM = _env_int("OCR_MAX_DIM", 1600)
# Cap de repli quand la 1re passe lève une bad-alloc ONNX.
_RETRY_MAX_DIM = _env_int("OCR_RETRY_MAX_DIM", 1024)
# Bornage interne du détecteur DBNet. Le défaut RapidOCR (limit_side_len=736,
# limit_type=min) n'agit jamais sur des captures hautes : min(w,h)=w est déjà
# > 736 → aucun resize. On force le mode "max" pour garantir une borne.
_DET_LIMIT_SIDE_LEN = _env_int("OCR_DET_LIMIT_SIDE_LEN", 1280)
_DET_LIMIT_TYPE = os.environ.get("OCR_DET_LIMIT_TYPE", "max")
_USE_CLS = _env_bool("OCR_USE_CLS", False)


def _is_bad_alloc(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "bad allocation" in msg or "bad_alloc" in msg


class _Engine:
    _instance: Optional["_Engine"] = None
    _init_lock = Lock()

    def __init__(self) -> None:
        from rapidocr import RapidOCR

        threads = _env_int("OCR_THREADS_PER_SESSION", -1)
        params = {
            "EngineConfig.onnxruntime.intra_op_num_threads": threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": threads,
            "Global.use_cls": _USE_CLS,
            "Det.limit_side_len": _DET_LIMIT_SIDE_LEN,
            "Det.limit_type": _DET_LIMIT_TYPE,
        }
        self._ocr = RapidOCR(params=params)
        # Sérialise les appels à self._ocr : les sessions ONNX sous-jacentes
        # ne sont pas garanties thread-safe selon le build, et plusieurs
        # threads FastAPI peuvent appeler instance() concurremment.
        self._run_lock = Lock()

    @classmethod
    def instance(cls) -> "_Engine":
        # On ne setter `_instance` qu'à la fin de __init__ (via une variable
        # locale) pour éviter qu'un thread voit l'instance partielle pendant
        # le chargement des modèles ONNX (~5-10 s).
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    tmp = cls.__new__(cls)
                    tmp.__init__()
                    cls._instance = tmp
        return cls._instance

    def run(self, image_bytes: bytes) -> OcrResult:
        return self._run_with_retry(image_bytes, boost_contrast=False)

    def run_with_contrast(self, image_bytes: bytes) -> OcrResult:
        """2e passe : image avec contraste boosté pour les Sedad arabes où le
        montant noir est noyé dans du texte arabe gris."""
        return self._run_with_retry(image_bytes, boost_contrast=True)

    def _run_with_retry(self, image_bytes: bytes, *, boost_contrast: bool) -> "OcrResult":
        img = self._preprocess(image_bytes, boost_contrast=boost_contrast)
        try:
            with self._run_lock:
                raw = self._ocr(img)
        except Exception as exc:
            if not _is_bad_alloc(exc):
                raise
            logger.warning(
                "ONNX bad_alloc sur image %sx%s, retry à max_dim=%d",
                img.shape[1], img.shape[0], _RETRY_MAX_DIM,
            )
            del img
            gc.collect()
            img = self._preprocess(
                image_bytes, boost_contrast=boost_contrast, max_dim=_RETRY_MAX_DIM
            )
            with self._run_lock:
                raw = self._ocr(img)
        return self._normalize(raw)

    @staticmethod
    def _preprocess(
        image_bytes: bytes,
        boost_contrast: bool = False,
        *,
        max_dim: Optional[int] = None,
    ) -> np.ndarray:
        cap = max_dim if max_dim is not None else _DEFAULT_MAX_DIM
        with Image.open(io.BytesIO(image_bytes)) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            # Borne 1 : largeur (rétrocompat OCR_MAX_WIDTH).
            max_w = _DEFAULT_MAX_WIDTH
            if im.width > max_w:
                ratio = max_w / im.width
                im = im.resize((max_w, int(im.height * ratio)), Image.LANCZOS)
            # Borne 2 : plus grande dimension. Indispensable pour les
            # screenshots verticaux dont la hauteur n'est pas couverte par
            # OCR_MAX_WIDTH.
            longest = max(im.width, im.height)
            if longest > cap:
                ratio = cap / longest
                im = im.resize(
                    (max(1, int(im.width * ratio)), max(1, int(im.height * ratio))),
                    Image.LANCZOS,
                )
            if boost_contrast:
                gray = ImageOps.grayscale(im)
                stretched = ImageOps.autocontrast(gray, cutoff=8)
                im = stretched.convert("RGB")
            return np.array(im)

    @staticmethod
    def _normalize(raw) -> OcrResult:
        boxes: List[OcrBox] = []
        if raw is None:
            return OcrResult(boxes=[], text="")

        bboxes = getattr(raw, "boxes", None)
        txts = getattr(raw, "txts", None)
        scores = getattr(raw, "scores", None)
        if txts is None:
            return OcrResult(boxes=[], text="")

        for i, txt in enumerate(txts):
            conf = float(scores[i]) if scores is not None and i < len(scores) else 0.0
            bbox = []
            if bboxes is not None and i < len(bboxes):
                try:
                    bbox = [list(map(float, p)) for p in bboxes[i]]
                except (TypeError, ValueError):
                    bbox = []
            boxes.append(OcrBox(text=str(txt), confidence=conf, bbox=bbox))

        text = " ".join(b.text for b in boxes)
        return OcrResult(boxes=boxes, text=text)


def warmup() -> None:
    _Engine.instance()


def is_ready() -> bool:
    """True seulement si l'instance singleton est complètement initialisée
    (modèles ONNX chargés). Lecture protégée par le lock de construction."""
    with _Engine._init_lock:
        return _Engine._instance is not None


def run_ocr(image_bytes: bytes) -> OcrResult:
    return _Engine.instance().run(image_bytes)


def run_ocr_high_contrast(image_bytes: bytes) -> OcrResult:
    """2e passe à contraste boosté : utile pour les Sedad arabes."""
    return _Engine.instance().run_with_contrast(image_bytes)


def confidence_for_span(result: OcrResult, value: str) -> float:
    """Meilleure confiance parmi les boîtes OCR dont le texte coïncide
    significativement avec ``value``.

    Pour éviter les faux positifs sur des sous-chaînes courtes (ex : ``"25"``
    matchait ``"2025"``), on exige que les deux compactés aient au moins 70%
    de longueur en commun.
    """
    if not value:
        return 0.0
    digits = "".join(ch for ch in value if ch.isalnum())
    if not digits:
        return 0.0
    n_target = len(digits)
    best = 0.0
    for box in result.boxes:
        compact = "".join(ch for ch in box.text if ch.isalnum())
        if not compact:
            continue
        # On exige que le plus court fasse au moins 70% du plus long, ce qui
        # élimine les recouvrements parasites du genre "25" ⊂ "2025".
        if digits in compact:
            if len(digits) >= 0.7 * len(compact):
                best = max(best, box.confidence)
        elif compact in digits:
            if len(compact) >= 0.7 * n_target:
                best = max(best, box.confidence)
    return best
