"""Rattrapage des faux 'sous-paiements' (MAC UCRM absent → clients laissés
suspendus à tort alors qu'ils avaient payé).

Contexte : l'API UCRM /clients/services renvoie parfois macAddress=null. Dans ce
cas plan_unblocks n'avait aucun MAC à débloquer (dû_total=0) et le client, bien
qu'à jour, restait suspendu. Le correctif pipeline (repli sur MAC locaux) évite
la récurrence ; ce script rattrape l'historique déjà passé.

Méthode :
  1. Re-parse data/logs/webhook.log pour lister les clients candidats
     (répartition dû_total=0 & débloqués=0).
  2. Pour chaque client, RE-VÉRIFIE l'état live :
       - solde dû actuel UCRM (accountOutstanding) ≤ tolérance ?
       - lignes locales encore statu=2 (suspendues) ?
  3. Ne débloque QUE les MAC encore suspendus d'un client à jour :
       - MikroTik : unblock_by_mac(mac)
       - DB       : update_client_status_by_mac(mac, statu=0)

Usage :
    python scripts/rattrapage_faux_sous_paiements.py            # DRY-RUN (défaut)
    python scripts/rattrapage_faux_sous_paiements.py --apply    # exécute les écritures
"""
from __future__ import annotations

import asyncio
import io
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from whatsapp_automation import config  # noqa: E402
from whatsapp_automation.db import postgres as pg  # noqa: E402
from whatsapp_automation.worker import ucrm, mikrotik  # noqa: E402

APPLY = "--apply" in sys.argv
THRESHOLD = config.UNDERPAYMENT_TOLERANCE
LOG = ROOT / "data" / "logs" / "webhook.log"
SUSPENDED_STATU = 2
ACTIVE_STATU = 0

rep_re = re.compile(r"répartition : abos_suspendus=\d+ dû_total=0 .*débloqués=0")
res_re = re.compile(r"pipeline result: \{.*'client_id': (?P<cid>\d+),")


def candidate_client_ids() -> list[int]:
    lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    ids: list[int] = []
    seen: set[int] = set()
    for i, ln in enumerate(lines):
        if not rep_re.search(ln):
            continue
        for j in range(i, min(i + 6, len(lines))):
            r = res_re.search(lines[j])
            if r:
                cid = int(r.group("cid"))
                if cid not in seen:
                    seen.add(cid)
                    ids.append(cid)
                break
    return ids


def valid_mac(mac) -> bool:
    m = (mac or "").strip()
    return bool(m) and not m.lower().startswith("pending-")


async def main() -> None:
    mode = "APPLY (écritures réelles)" if APPLY else "DRY-RUN (aucune écriture)"
    print(f"=== Rattrapage faux sous-paiements — {mode} — tolérance={THRESHOLD} ===\n")

    candidates = candidate_client_ids()
    print(f"{len(candidates)} clients candidats depuis les logs.\n")

    to_unblock = 0          # lignes MAC débloquées
    clients_unblocked = 0   # clients traités
    skipped_owing = 0       # solde encore dû > tolérance
    skipped_active = 0      # déjà actif localement
    errors = 0

    for cid in candidates:
        try:
            balance = await ucrm.get_balance(cid)
            rows = pg.get_client_by_id(cid)
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"[ERR ] client={cid} : {e}")
            continue

        susp_macs = sorted({
            (r.get("mac") or "").strip()
            for r in rows
            if r.get("statu") == SUSPENDED_STATU and valid_mac(r.get("mac"))
        })

        if balance > THRESHOLD:
            skipped_owing += 1
            print(f"[SKIP] client={cid} solde_dû={balance} > {THRESHOLD} → pas à jour, on laisse")
            continue
        if not susp_macs:
            skipped_active += 1
            print(f"[ OK ] client={cid} solde_dû={balance} — aucune ligne statu=2 (déjà actif)")
            continue

        clients_unblocked += 1
        for mac in susp_macs:
            to_unblock += 1
            if APPLY:
                removed = await mikrotik.unblock_by_mac(mac)
                nrows = pg.update_client_status_by_mac(mac, ACTIVE_STATU)
                print(f"[DONE] client={cid} solde_dû={balance} mac={mac} "
                      f"→ MikroTik rules_removed={removed}, statu→0 lignes={nrows}")
            else:
                print(f"[PLAN] client={cid} solde_dû={balance} mac={mac} → débloquerait")

    print("\n=== Résumé ===")
    print(f"  Clients à débloquer   : {clients_unblocked}")
    print(f"  MAC concernés         : {to_unblock}")
    print(f"  Ignorés (encore dû)   : {skipped_owing}")
    print(f"  Ignorés (déjà actifs) : {skipped_active}")
    print(f"  Erreurs               : {errors}")
    if not APPLY:
        print("\n(DRY-RUN — relancer avec --apply pour exécuter les déblocages)")


if __name__ == "__main__":
    asyncio.run(main())
