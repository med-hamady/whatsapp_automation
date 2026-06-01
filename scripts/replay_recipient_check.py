"""Rejoue validate_recipient_name sur toutes les captures déjà OCRisées.

Ne relance PAS l'OCR : on lit prediction.json (template + raw_text) déjà
persisté par le service. Affiche un rapport par template avec :
- nombre total
- nombre accepté / rejeté
- liste détaillée des rejets (sample_id + extrait raw_text)

Permet d'estimer le taux de faux rejets AVANT de déployer le check.
"""

from __future__ import annotations

import io
import json
import sys
from collections import defaultdict
from pathlib import Path

# Force UTF-8 sur stdout/stderr (Windows console est en cp1252 par défaut et
# crashe sur arabe / sigles asiatiques fréquents dans les OCR bruités).
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from whatsapp_automation.webhook.validators import validate_recipient_name  # noqa: E402


STORE = ROOT / "data" / "dataset" / "store"


def main() -> int:
    by_template: dict[str, list[tuple[str, str, bool, str]]] = defaultdict(list)
    total = 0

    for pred_path in STORE.rglob("prediction.json"):
        try:
            with pred_path.open("r", encoding="utf-8") as f:
                pred = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"!! skip {pred_path}: {exc}", file=sys.stderr)
            continue

        template = pred.get("template", "")
        raw_text = pred.get("raw_text", "") or ""
        sample_id = pred_path.parent.name

        res = validate_recipient_name(template, raw_text)
        by_template[template].append((sample_id, raw_text, res.ok, res.reason or ""))
        total += 1

    print(f"=== {total} captures analysées ===\n")
    grand_ok = grand_ko = 0
    for tpl in sorted(by_template):
        items = by_template[tpl]
        ok = sum(1 for it in items if it[2])
        ko = len(items) - ok
        grand_ok += ok
        grand_ko += ko
        rate = (ko / len(items) * 100) if items else 0
        print(f"[{tpl}] total={len(items)} acceptés={ok} rejetés={ko} ({rate:.1f}% rejets)")

    print(f"\nTOTAL acceptés={grand_ok} rejetés={grand_ko} "
          f"({grand_ko/total*100:.1f}% rejets)" if total else "")

    print("\n=== DÉTAIL DES REJETS ===")
    for tpl in sorted(by_template):
        rejected = [it for it in by_template[tpl] if not it[2]]
        if not rejected:
            continue
        print(f"\n--- [{tpl}] {len(rejected)} rejet(s) ---")
        for sample_id, raw_text, _ok, reason in rejected:
            snippet = raw_text[:160].replace("\n", " ")
            print(f"  {sample_id}  reason={reason}")
            print(f"    raw: {snippet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
