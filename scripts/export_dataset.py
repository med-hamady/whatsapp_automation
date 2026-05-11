"""Exporte les échantillons annotés vers un fichier JSONL utilisable pour
fine-tuning futur (Donut, LayoutLMv3, ou un OCR custom).

Chaque ligne du JSONL contient :
    {
      "sample_id": "2026-05-05/abc123",
      "image_path": "<absolute_path>/image.jpg",
      "ocr": {...},                  ← sortie PaddleOCR brute
      "prediction": {...},           ← prédiction du modèle au moment de la collecte
      "label": {                     ← vérité terrain saisie par l'opérateur
        "montant": 1500,
        "txn_id": "...",
        "date_heure": "2025-11-26T16:54:59",
        "operator": "bankily",
        "valid": true,
        "notes": null
      }
    }

Usage :
    python -m ai_ocr.scripts.export_dataset --out training.jsonl [--include-unlabeled]
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(_ROOT / 'src'))
_sys.path.insert(0, str(_ROOT))


import argparse
import json
from pathlib import Path

from whatsapp_automation.ai_ocr.dataset.writer import iter_samples, sample_dir


def main():
    parser = argparse.ArgumentParser(description="Exporte le dataset au format JSONL")
    parser.add_argument("--out", required=True, help="Chemin du fichier .jsonl de sortie")
    parser.add_argument(
        "--include-unlabeled",
        action="store_true",
        help="Inclure aussi les échantillons sans label (pour pseudo-labelling)",
    )
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with out_path.open("w", encoding="utf-8") as f:
        for sample in iter_samples(only_unlabeled=False):
            if not sample.get("labeled") and not args.include_unlabeled:
                continue
            sd = sample_dir(sample["sample_id"])
            ocr_path = sd / "ocr.json"
            ocr_payload = (
                json.loads(ocr_path.read_text(encoding="utf-8"))
                if ocr_path.exists()
                else None
            )
            entry = {
                "sample_id": sample["sample_id"],
                "image_path": str(sd / "image.jpg"),
                "ocr": ocr_payload,
                "prediction": sample["prediction"],
                "label": sample["label"],
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            count += 1

    print(f"{count} echantillon(s) ecrit(s) dans {out_path}")


if __name__ == "__main__":
    main()
