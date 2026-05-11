"""Test sur-mesure : phone 48783201, facture 1000 MRU, paie 700 MRU.

Écart = 1000 - 700 = 300 > seuil 150 → paiement enregistré mais
client NON débloqué. Message WhatsApp : "il reste 300 MRU à payer".

Prérequis : init_db --seed, fakes lancés (9001/9002/9003), worker lancé,
et le client a été inséré dans Postgres avec idclient=5.
"""
from __future__ import annotations

import sys as _sys
import time
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(_ROOT / "src"))
_sys.path.insert(0, str(_ROOT))

import httpx

from whatsapp_automation import config
from whatsapp_automation.jobqueue import store as queue_store
from whatsapp_automation.models import Client, Job, Payment, Source


CLIENT_ID = 5
PHONE = "48783201"
MAC = "AA:BB:CC:00:00:99"
FIREWALL_RULE_ID = "*9Z"
BALANCE = 1000
PAID = 700


def main() -> None:
    queue_store.init_db()

    diff = BALANCE - PAID
    should_unblock = diff <= config.UNDERPAYMENT_TOLERANCE
    txn_id = f"TEST-48783201-{int(time.time())}"

    print("=" * 70)
    print(f"TEST sous-paiement client {PHONE}")
    print(f"  balance UCRM = {BALANCE} MRU")
    print(f"  payé         = {PAID} MRU")
    print(f"  écart        = {diff} MRU (seuil tolérance = {config.UNDERPAYMENT_TOLERANCE})")
    print(f"  → should_unblock = {should_unblock}")
    print("=" * 70)

    job = Job(
        job_id=queue_store.new_job_id(),
        client=Client(
            id=CLIENT_ID,
            phone=PHONE,
            mac_address=MAC,
            current_status="suspended",
            firewall_rule_id=FIREWALL_RULE_ID,
        ),
        payment=Payment(
            amount_mru=PAID,
            txn_id=txn_id,
            operator="bankily",
            crm_balance_before=BALANCE,
            should_unblock=should_unblock,
        ),
        source=Source(
            wnum=PHONE,
            sample_id="2026-05-11/demo-48783201",
            received_at="2026-05-11T10:00:00Z",
        ),
    )
    queue_store.enqueue(job)
    print(f"\n[enqueued] job_id={job.job_id} txn_id={txn_id}")

    print("\n--- Attente worker (timeout 30s) ---")
    for i in range(60):
        time.sleep(0.5)
        if queue_store.is_txn_processed(txn_id):
            print(f"  ✅ traité après {(i + 1) * 0.5:.1f}s")
            break
    else:
        print("  ✗ TIMEOUT — worker pas démarré ?")
        return

    print("\n--- Vérifications ---")
    ucrm_payments = httpx.get(
        f"{config.UCRM_BASE_URL}/payments",
        headers={"X-Auth-App-Key": config.UCRM_APP_KEY},
    ).json()
    mt_rules = httpx.get(f"{config.MIKROTIK_BASE_URL}/firewall/rules").json()
    msgs = httpx.get(f"{config.ULTRAMSG_BASE_URL}/messages").json()

    print("\n[UCRM] paiements créés :")
    for p in ucrm_payments:
        print(f"   {p}")

    print(f"\n[MikroTik] règle {FIREWALL_RULE_ID} encore présente ?")
    rule_present = any(r["id"] == FIREWALL_RULE_ID for r in mt_rules)
    expected_present = not should_unblock
    status = "✅" if rule_present == expected_present else "❌"
    print(f"   {status} présente={rule_present} (attendu={expected_present})")

    print(f"\n[UltraMsg] messages envoyés à +222{PHONE} :")
    for m in msgs:
        if PHONE in m.get("to", ""):
            print(f"   to={m['to']}")
            print(f"   caption=\"{m.get('caption', '')}\"")

    import psycopg
    print(f"\n[Postgres] table paiements pour client {CLIENT_ID} :")
    with psycopg.connect(config.DATABASE_URL) as conn:
        cur = conn.execute(
            "SELECT id, idclient, montant, ucrm_payment_id, txn_id, operator "
            "FROM paiements WHERE idclient = %s ORDER BY id DESC LIMIT 5",
            (CLIENT_ID,),
        )
        for row in cur.fetchall():
            print(f"   {row}")

    print(f"\n[Postgres] statut client {CLIENT_ID} :")
    with psycopg.connect(config.DATABASE_URL) as conn:
        cur = conn.execute(
            "SELECT idclient, num, statu FROM clients WHERE idclient = %s",
            (CLIENT_ID,),
        )
        print(f"   {cur.fetchone()}  (statu=1 suspendu, 0 actif)")


if __name__ == "__main__":
    main()
