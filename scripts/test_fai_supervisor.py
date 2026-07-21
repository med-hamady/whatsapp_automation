"""Test manuel du client superviseur LR (API FAI).

Tape sur le VRAI superviseur (celui de FAI_API_BASE_URL / FAI_API_KEY dans .env).

Le système de paiement ne pose JAMAIS de coupure : ce script n'expose donc que
les deux actions dont il a besoin.

    # Lecture seule — état d'une MAC (ne touche pas au LR)
    python scripts/test_fai_supervisor.py status d0:21:f9:f6:07:c2

    # Déblocage réel d'une MAC (rétablit l'accès — sans effet si déjà actif)
    python scripts/test_fai_supervisor.py unblock d0:21:f9:f6:07:c2

Rappel : `ok: false` n'est pas un échec — le LR est momentanément injoignable,
l'ordre est enregistré côté superviseur et sera ré-appliqué automatiquement.
C'est `client_blocked` qui porte l'intention.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from whatsapp_automation import config  # noqa: E402
from whatsapp_automation.worker import fai_supervisor  # noqa: E402


def show(label: str, res: dict) -> None:
    print(f"--- {label}")
    print(json.dumps(res, indent=2, ensure_ascii=False))
    print()


async def run(action: str, mac: str) -> int:
    if not fai_supervisor.enabled():
        print("Superviseur non configuré : renseigner FAI_API_BASE_URL et "
              "FAI_API_KEY dans .env")
        return 2

    print(f"Superviseur : {config.FAI_API_BASE_URL}  (TLS vérifié: "
          f"{config.FAI_API_VERIFY_SSL})")
    print(f"MAC         : {mac}\n")

    try:
        if action == "status":
            show("status", await fai_supervisor.get_status_by_mac(mac))
        else:
            show("status avant", await fai_supervisor.get_status_by_mac(mac))
            show("unblock", await fai_supervisor.unblock_by_mac(mac))
            show("status après", await fai_supervisor.get_status_by_mac(mac))
    except fai_supervisor.FaiSupervisorError as exc:
        print(f"ECHEC (http={exc.status_code}) : {exc}")
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["status", "unblock"])
    p.add_argument("mac")
    args = p.parse_args()
    return asyncio.run(run(args.action, args.mac))


if __name__ == "__main__":
    sys.exit(main())
