"""Sauvegarde de chaque image traitée pour annotation et fine-tuning futur.

Structure :
    dataset/store/{YYYY-MM-DD}/{uuid4}/
        ├── image.jpg            ← image brute reçue
        ├── ocr.json             ← sortie PaddleOCR (boxes + scores)
        ├── prediction.json      ← extracted + confidence + template
        └── label.json           ← absent tant que non annoté par un humain

Sécurité : tout `sample_id` reçu de l'extérieur est validé via
``_safe_sample_dir`` pour éviter le path traversal (``../../etc/passwd``).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Iterator, Optional

from ... import config


_STORE_ROOT = Path(config.AI_OCR_DATASET_PATH) / "store"

# Un sample_id valide a la forme exacte "YYYY-MM-DD/<hex32>" (ce qui est
# produit par save_sample). Toute autre chaîne est refusée.
_SAMPLE_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}/[0-9a-f]{32}$")


def store_root() -> Path:
    _STORE_ROOT.mkdir(parents=True, exist_ok=True)
    return _STORE_ROOT


def _safe_sample_dir(sample_id: str) -> Optional[Path]:
    """Retourne le path absolu du sample s'il est valide ET sous le store,
    None sinon. Empêche tout path traversal."""
    if not isinstance(sample_id, str) or not _SAMPLE_ID_RE.match(sample_id):
        return None
    candidate = (store_root() / sample_id).resolve()
    try:
        candidate.relative_to(store_root().resolve())
    except ValueError:
        return None
    return candidate


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def save_sample(image_bytes: bytes, ocr_payload: dict, prediction: dict) -> str:
    today = date.today().isoformat()
    sample_uuid = uuid.uuid4().hex
    sample_dir_path = store_root() / today / sample_uuid
    sample_dir_path.mkdir(parents=True, exist_ok=True)

    (sample_dir_path / "image.jpg").write_bytes(image_bytes)
    (sample_dir_path / "ocr.json").write_text(
        json.dumps(ocr_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (sample_dir_path / "prediction.json").write_text(
        json.dumps(
            {**prediction, "saved_at": _utc_now_iso()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return f"{today}/{sample_uuid}"


def sample_dir(sample_id: str) -> Path:
    """Retourne le path du sample. **Lève ValueError** si sample_id est
    invalide (caractères dangereux, traversal, format incorrect)."""
    sd = _safe_sample_dir(sample_id)
    if sd is None:
        raise ValueError(f"invalid sample_id: {sample_id!r}")
    return sd


def load_sample(sample_id: str) -> Optional[dict]:
    sd = _safe_sample_dir(sample_id)
    if sd is None:
        return None
    pred_path = sd / "prediction.json"
    if not pred_path.exists():
        return None
    label_path = sd / "label.json"
    return {
        "sample_id": sample_id,
        "prediction": json.loads(pred_path.read_text(encoding="utf-8")),
        "label": (
            json.loads(label_path.read_text(encoding="utf-8"))
            if label_path.exists()
            else None
        ),
        "image_path": str(sd / "image.jpg"),
    }


def write_label(sample_id: str, label: dict) -> bool:
    sd = _safe_sample_dir(sample_id)
    if sd is None or not sd.exists():
        return False
    payload = {**label, "annotated_at": _utc_now_iso()}
    (sd / "label.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return True


def iter_samples(only_unlabeled: bool = False) -> Iterator[dict]:
    root = store_root()
    if not root.exists():
        return
    days = sorted([d for d in root.iterdir() if d.is_dir()], reverse=True)
    for day in days:
        for sample in sorted(day.iterdir(), reverse=True):
            if not sample.is_dir():
                continue
            sample_id = f"{day.name}/{sample.name}"
            label_exists = (sample / "label.json").exists()
            if only_unlabeled and label_exists:
                continue
            data = load_sample(sample_id)
            if data is None:
                continue
            data["labeled"] = label_exists
            yield data
