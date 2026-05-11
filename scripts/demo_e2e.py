"""Démonstration end-to-end du pipeline (sans image réelle).

Ce script :
  1. Vérifie que les fakes tournent (UCRM 9001, MikroTik 9002, UltraMsg 9003).
  2. Crée un Job synthétique (comme si l'OCR avait déjà tourné) et l'empile
     directement dans la queue.
  3. Affiche en temps réel l'état avant/après pour valider que le worker
     fait bien son boulot.

Prérequis : init_db --seed déjà exécuté + worker en cours d'exécution dans
une autre fenêtre.
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


def _check_service(url: str, label: str) -> bool:
    try:
        r = httpx.get(url, timeout=2.0)
        print(f"  ✓ {label} ({url}) → HTTP {r.status_code}")
        return True
    except Exception as exc:
        print(f"  ✗ {label} ({url}) → {exc}")
        return False


def main():
    print("=" * 60)
    print("DEMO E2E — pipeline WhatsApp Python")
    print("=" * 60)

    print("\n[1] Vérification des services :")
    all_ok = all([
        _check_service(f"{config.UCRM_BASE_URL}/health", "fake UCRM"),
        _check_service(f"{config.MIKROTIK_BASE_URL}/health", "fake MikroTik"),
        _check_service(f"{config.ULTRAMSG_BASE_URL}/health", "fake UltraMsg"),
    ])
    if not all_ok:
        print("\n⚠ Lance d'abord : whatsapp_automation\\scripts\\run_fakes.bat\n")
        return

    auth = {"X-Auth-App-Key": config.UCRM_APP_KEY}
    print("\n[2] État initial :")
    rules_before = httpx.get(f"{config.MIKROTIK_BASE_URL}/firewall/rules").json()
    payments_before = httpx.get(f"{config.UCRM_BASE_URL}/payments", headers=auth).json()
    print(f"  - règles firewall : {len(rules_before)} → {[r['id'] for r in rules_before]}")
    print(f"  - paiements UCRM : {len(payments_before)}")

    print("\n[3] Construction d'un Job synthétique (client #1, 1500 MRU, Bankily) :")
    job = Job(
        job_id=queue_store.new_job_id(),
        client=Client(
            id=1,
            phone="37697850",
            mac_address="AA:BB:CC:00:00:01",
            current_status="suspended",
            firewall_rule_id="*1A",
        ),
        payment=Payment(
            amount_mru=1500,
            txn_id=f"DEMO-{int(time.time())}",
            date_heure="2026-05-11T10:00:00",
            operator="bankily",
        ),
        source=Source(
            wnum="37697850",
            sample_id="2026-05-11/demo",
            received_at="2026-05-11T10:00:00Z",
        ),
    )
    queue_store.init_db()
    internal_id = queue_store.enqueue(job)
    print(f"  → job empilé id={internal_id} job_id={job.job_id} txn={job.payment.txn_id}")

    print("\n[4] Attente du worker (timeout 30 s)…")
    for i in range(60):
        time.sleep(0.5)
        if queue_store.is_txn_processed(job.payment.txn_id):
            print(f"  ✅ job traité après {(i+1)*0.5:.1f}s")
            break
    else:
        print("  ✗ timeout — le worker tourne-t-il ?")
        return

    print("\n[5] État après traitement :")
    rules_after = httpx.get(f"{config.MIKROTIK_BASE_URL}/firewall/rules").json()
    payments_after = httpx.get(f"{config.UCRM_BASE_URL}/payments", headers=auth).json()
    messages = httpx.get(f"{config.ULTRAMSG_BASE_URL}/messages").json()
    print(f"  - règles firewall : {len(rules_after)} (avant {len(rules_before)})")
    print(f"    → règle *1A supprimée : {'OUI' if all(r['id'] != '*1A' for r in rules_after) else 'NON'}")
    print(f"  - paiements UCRM créés : {len(payments_after) - len(payments_before)}")
    if payments_after:
        last_payment = payments_after[-1]
        print(f"    → dernier : clientId={last_payment['clientId']} amount={last_payment['amount']}")
    print(f"  - messages WhatsApp envoyés : {len(messages)}")
    if messages:
        last_msg = messages[-1]
        print(f"    → dernier : to={last_msg['to']} document={last_msg['document']}")

    print("\n[6] Queue stats :", queue_store.stats())
    print("\n✅ DEMO terminée avec succès")


if __name__ == "__main__":
    main()
