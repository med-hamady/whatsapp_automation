"""Démonstration de la règle métier sous-paiement / sur-paiement / paiement exact.

Scénarios testés :
  1. Client #1 doit 1500. Paie 1500 → unblock + paiement enregistré.
  2. Client #2 doit 1190. Paie 1000 (sous-paiement 190 > 150) → paiement enregistré, PAS unblock.
  3. Client #3 doit  990. Paie 1200 (sur-paiement) → unblock + 210 MRU d'avoir.

Prérequis : init_db --seed, fakes, worker en cours.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(_ROOT / 'src'))
_sys.path.insert(0, str(_ROOT))


import time

import httpx

from whatsapp_automation import config
from whatsapp_automation.models import Client, Job, Payment, Source
from whatsapp_automation.jobqueue import store as queue_store


SCENARIOS = [
    {
        "label": "Paiement exact (client #1 : 1500 dus, paie 1500)",
        "client": {
            "id": 1, "phone": "37697850", "mac": "AA:BB:CC:00:00:01",
            "ip": "10.0.0.1", "rule_id": "*1A",
        },
        "balance_crm": 1500, "amount_paid": 1500,
        "expect_unblock": True,
    },
    {
        "label": "Sous-paiement (client #2 : 1190 dus, paie 1000, écart 190 > 150)",
        "client": {
            "id": 2, "phone": "33848414", "mac": "AA:BB:CC:00:00:02",
            "ip": "10.0.0.2", "rule_id": "*2B",
        },
        "balance_crm": 1190, "amount_paid": 1000,
        "expect_unblock": False,
    },
    {
        "label": "Sur-paiement (client #3 : 990 dus, paie 1200, avoir 210)",
        "client": {
            "id": 3, "phone": "49593871", "mac": "AA:BB:CC:00:00:03",
            "ip": "10.0.0.3", "rule_id": "*3C",
        },
        "balance_crm": 990, "amount_paid": 1200,
        "expect_unblock": True,
    },
]


def _build_job(s: dict) -> Job:
    underpayment = s["balance_crm"] - s["amount_paid"]
    should_unblock = underpayment <= config.UNDERPAYMENT_TOLERANCE
    return Job(
        job_id=queue_store.new_job_id(),
        client=Client(
            id=s["client"]["id"],
            phone=s["client"]["phone"],
            mac_address=s["client"]["mac"],
            ip_address=s["client"]["ip"],
            current_status="suspended",
        ),
        payment=Payment(
            amount_mru=s["amount_paid"],
            txn_id=f"DEMO-{int(time.time())}-{s['client']['id']}",
            operator="bankily",
            crm_balance_before=s["balance_crm"],
            should_unblock=should_unblock,
        ),
        source=Source(
            wnum=s["client"]["phone"],
            sample_id="2026-05-11/demo",
            received_at="2026-05-11T10:00:00Z",
        ),
    )


def main():
    queue_store.init_db()

    print("=" * 70)
    print("DEMO — règle métier sous-paiement / sur-paiement")
    print(f"Seuil de tolérance : {config.UNDERPAYMENT_TOLERANCE} MRU")
    print("=" * 70)

    enqueued_jobs = []
    for s in SCENARIOS:
        job = _build_job(s)
        queue_store.enqueue(job)
        enqueued_jobs.append((s, job))
        diff = s["balance_crm"] - s["amount_paid"]
        print(f"\n[Empilé] {s['label']}")
        print(f"  → balance={s['balance_crm']} paid={s['amount_paid']} "
              f"écart={diff:+d} → unblock={'OUI' if job.payment.should_unblock else 'NON'}")

    print("\n--- Attente du worker (timeout 30s) ---")
    for i in range(60):
        time.sleep(0.5)
        all_done = all(queue_store.is_txn_processed(j.payment.txn_id) for _, j in enqueued_jobs)
        if all_done:
            print(f"  ✅ Tous les jobs traités après {(i+1)*0.5:.1f}s")
            break
    else:
        print("  ✗ timeout — worker pas démarré ?")
        return

    print("\n--- Vérifications ---\n")
    rules = httpx.get(f"{config.MIKROTIK_BASE_URL}/firewall/rules").json()
    rule_ids = {r["id"] for r in rules}
    messages = httpx.get(f"{config.ULTRAMSG_BASE_URL}/messages").json()

    for s, job in enqueued_jobs:
        rule_id = s["client"]["rule_id"]
        rule_removed = rule_id not in rule_ids
        expected = job.payment.should_unblock
        client_msg = next(
            (m for m in messages if s["client"]["phone"] in m["to"]),
            None,
        )

        status = "✅" if rule_removed == expected else "❌"
        print(f"{status} {s['label']}")
        print(f"   - règle {rule_id} {'supprimée' if rule_removed else 'PRÉSENTE'} "
              f"(attendu : {'supprimée' if expected else 'présente'})")
        if client_msg:
            print(f"   - message WhatsApp envoyé : \"{client_msg['caption']}\"")


if __name__ == "__main__":
    main()
