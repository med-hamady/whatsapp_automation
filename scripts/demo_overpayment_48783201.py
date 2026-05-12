"""Test sur-paiement client 48783201 (idclient=1 dans le seed).

Solde dû UCRM = 900 MRU, le client paie 1000 MRU.
Écart = 900 - 1000 = -100 → avoir 100 MRU, must unblock.

Prérequis : fakes 9001/9002/9003 lancés, worker lancé, DB seedée.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

import httpx
import psycopg

from whatsapp_automation import config
from whatsapp_automation.jobqueue import store as queue_store
from whatsapp_automation.models import Client, Job, Payment, Source


CLIENT_ID = 1
PHONE = "48783201"
MAC = "AA:BB:CC:00:00:01"
IP_ADDRESS = "10.0.0.1"
BALANCE = 900
PAID = 1000


def main() -> None:
    queue_store.init_db()

    diff = BALANCE - PAID
    should_unblock = diff <= config.UNDERPAYMENT_TOLERANCE
    avoir = max(0, -diff)
    txn_id = f"OVR-{PHONE}-{int(time.time())}"

    print("=" * 70)
    print(f"TEST sur-paiement client {PHONE}")
    print(f"  balance UCRM = {BALANCE} MRU")
    print(f"  payé         = {PAID} MRU")
    print(f"  écart        = {diff} MRU (avoir = {avoir} MRU)")
    print(f"  → should_unblock = {should_unblock}")
    print("=" * 70)

    job = Job(
        job_id=queue_store.new_job_id(),
        client=Client(
            id=CLIENT_ID,
            phone=PHONE,
            mac_address=MAC,
            ip_address=IP_ADDRESS,
            current_status="suspended",
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
            sample_id="2026-05-12/demo-over-48783201",
            received_at="2026-05-12T10:00:00Z",
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

    # UCRM : dernier paiement pour ce client
    pays_for_client = [p for p in ucrm_payments if p.get("clientId") == CLIENT_ID]
    print(f"\n[UCRM] paiements pour client {CLIENT_ID} :")
    for p in pays_for_client[-3:]:
        print(f"   id={p.get('id')} amount={p.get('amount')} method={p.get('methodId','?')[:8]} note={p.get('note')!r}")

    # MikroTik : règle bloquant cette MAC encore présente ?
    rules_for_mac = [r for r in mt_rules if r.get("mac_address","").upper() == MAC.upper()]
    print(f"\n[MikroTik] règles bloquant {MAC} :")
    if rules_for_mac:
        for r in rules_for_mac:
            print(f"   ❌ {r}")
    else:
        print(f"   ✅ aucune (règle supprimée comme attendu)")

    # UltraMsg : message envoyé à +222<phone>
    msgs_for_client = [m for m in msgs if PHONE in m.get("to","")]
    print(f"\n[UltraMsg] messages à +222{PHONE} :")
    for m in msgs_for_client[-2:]:
        body = m.get("body") or m.get("caption","")
        print(f"   to={m['to']}")
        for line in (body or "").splitlines():
            print(f"     {line}")

    # DB locale
    print(f"\n[Postgres] paiment pour client {CLIENT_ID} :")
    with psycopg.connect(config.DATABASE_URL) as conn:
        cur = conn.execute(
            "SELECT id_payment, idclient, amount, phone, day, month, year, txn_id "
            "FROM paiment WHERE idclient = %s ORDER BY day DESC, month DESC LIMIT 3",
            (CLIENT_ID,),
        )
        for row in cur.fetchall():
            print(f"   {row}")

    print(f"\n[Postgres] statut client {CLIENT_ID} :")
    with psycopg.connect(config.DATABASE_URL) as conn:
        cur = conn.execute(
            "SELECT idclient, info, statu, mac, ipaddress FROM client WHERE idclient = %s",
            (CLIENT_ID,),
        )
        row = cur.fetchone()
        statu = row[2]
        mark = "✅ actif" if statu == 0 else f"❌ statu={statu}"
        print(f"   {row}  → {mark}")


if __name__ == "__main__":
    main()
