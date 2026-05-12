"""Test fake sur 2 clients :
  - 48783201 : paie avec 150 MRU restants (= seuil tolérance) → DÉBLOQUÉ
  - 46603985 : paie en sous-paiement > 150        → enregistré mais NON DÉBLOQUÉ

Le script :
 1. Insère/upsert les 2 clients dans la DB locale (idclient 5 et 6).
 2. Crée les 2 rules firewall correspondantes côté fake MikroTik (idempotent).
 3. Construit 2 Jobs synthétiques et les empile dans la queue.
 4. Attend que le worker les traite, puis vérifie l'état final.

Prérequis : fakes (9001/9002/9003) + worker en cours d'exécution, et
init_db --reset --seed déjà passé.
"""
from __future__ import annotations

import sys as _sys
import time
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(_ROOT / "src"))
_sys.path.insert(0, str(_ROOT))

import httpx
import psycopg

from whatsapp_automation import config
from whatsapp_automation.jobqueue import store as queue_store
from whatsapp_automation.models import Client, Job, Payment, Source


SCENARIOS = [
    {
        "label": "48783201 — sous-paiement TOLÉRÉ (écart = seuil 150)",
        "idclient": 5,
        "phone": "48783201",
        "mac": "AA:BB:CC:00:00:05",
        "ip": "10.0.0.5",
        "rule_id": "*5E",
        "balance": 900,
        "paid": 750,            # diff = 150 → unblock=True
    },
    {
        "label": "46603985 — sous-paiement EXCESSIF (écart 300 > seuil 150)",
        "idclient": 6,
        "phone": "46603985",
        "mac": "AA:BB:CC:00:00:06",
        "ip": "10.0.0.6",
        "rule_id": "*6F",
        "balance": 1000,
        "paid": 700,            # diff = 300 → unblock=False
    },
]


def upsert_client(s: dict) -> None:
    """Insère (ou met à jour) le client en DB locale avec statu=2 (suspendu — code prod)."""
    info = f"Client Test {s['phone']}"
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        cur = conn.execute("SELECT 1 FROM client WHERE idclient = %s", (s["idclient"],))
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO client (idclient, info, mac, statu, ipaddress) "
                "VALUES (%s, %s, %s, 2, %s)",
                (s["idclient"], info, s["mac"], s["ip"]),
            )
            print(f"  + client {s['phone']} (id={s['idclient']}) inséré")
        else:
            conn.execute(
                "UPDATE client SET info=%s, mac=%s, statu=2, ipaddress=%s "
                "WHERE idclient=%s",
                (info, s["mac"], s["ip"], s["idclient"]),
            )
            print(f"  ~ client {s['phone']} (id={s['idclient']}) déjà présent, statu=2 forcé")
        # On nettoie d'éventuels paiements précédents avec le même txn_id
        # pour permettre de relancer le test plusieurs fois (la queue, elle,
        # est protégée par processed_payments).


def upsert_rule(s: dict) -> None:
    """Crée la rule firewall correspondante côté fake MikroTik (idempotent)."""
    payload = {
        "id": s["rule_id"],
        "mac_address": s["mac"],
        "src_address": s["ip"],
        "comment": f"Suspended {s['phone']}",
    }
    r = httpx.post(f"{config.MIKROTIK_BASE_URL}/firewall/rules", json=payload, timeout=5)
    if r.status_code in (200, 201):
        print(f"  + rule {s['rule_id']} (ip {s['ip']}) créée")
    else:
        print(f"  ! rule {s['rule_id']} : HTTP {r.status_code} {r.text[:80]}")


def build_job(s: dict) -> Job:
    diff = s["balance"] - s["paid"]
    unblock = diff <= config.UNDERPAYMENT_TOLERANCE
    return Job(
        job_id=queue_store.new_job_id(),
        client=Client(
            id=s["idclient"],
            phone=s["phone"],
            mac_address=s["mac"],
            ip_address=s["ip"],
            current_status="suspended",
        ),
        payment=Payment(
            amount_mru=s["paid"],
            txn_id=f"TEST-{s['phone']}-{int(time.time())}",
            operator="bankily",
            crm_balance_before=s["balance"],
            should_unblock=unblock,
        ),
        source=Source(
            wnum=s["phone"],
            sample_id=f"2026-05-12/{s['phone']}",
            received_at="2026-05-12T10:00:00Z",
        ),
    )


def main() -> None:
    queue_store.init_db()

    print("=" * 70)
    print("TEST FAKE — 2 clients (48783201 + 46603985)")
    print(f"Seuil de tolérance UNDERPAYMENT = {config.UNDERPAYMENT_TOLERANCE} MRU")
    print("=" * 70)

    print("\n[1] Préparation DB + MikroTik")
    for s in SCENARIOS:
        upsert_client(s)
        upsert_rule(s)

    print("\n[2] Construction et enqueue des 2 jobs")
    jobs = []
    for s in SCENARIOS:
        job = build_job(s)
        queue_store.enqueue(job)
        jobs.append((s, job))
        diff = s["balance"] - s["paid"]
        decision = "UNBLOCK" if job.payment.should_unblock else "KEEP SUSPENDED"
        print(f"  → {s['phone']}: balance={s['balance']} payé={s['paid']} "
              f"écart={diff} → {decision}")

    print("\n[3] Attente du worker (timeout 30 s)")
    for i in range(60):
        time.sleep(0.5)
        all_done = all(queue_store.is_txn_processed(j.payment.txn_id) for _, j in jobs)
        if all_done:
            print(f"  ✅ Tous les jobs traités après {(i + 1) * 0.5:.1f}s")
            break
    else:
        print("  ✗ TIMEOUT — le worker tourne-t-il ?")
        return

    print("\n[4] Vérifications état final\n")
    auth = {"X-Auth-App-Key": config.UCRM_APP_KEY}
    ucrm_payments = httpx.get(f"{config.UCRM_BASE_URL}/payments", headers=auth).json()
    mt_rules = httpx.get(f"{config.MIKROTIK_BASE_URL}/firewall/rules").json()
    rule_ids_present = {r["id"] for r in mt_rules}
    msgs = httpx.get(f"{config.ULTRAMSG_BASE_URL}/messages").json()

    for s, job in jobs:
        print(f"--- {s['label']} ---")
        # a. statu DB
        with psycopg.connect(config.DATABASE_URL) as conn:
            cur = conn.execute("SELECT statu FROM client WHERE idclient=%s", (s["idclient"],))
            statu = cur.fetchone()[0]
        expected_statu = 0 if job.payment.should_unblock else 2
        ok_statu = statu == expected_statu
        print(f"  statu DB        : {statu} (attendu {expected_statu}) {'✅' if ok_statu else '❌'}")

        # b. rule MikroTik
        rule_removed = s["rule_id"] not in rule_ids_present
        ok_rule = rule_removed == job.payment.should_unblock
        print(f"  rule {s['rule_id']:5s} MT  : "
              f"{'supprimée' if rule_removed else 'présente'} "
              f"(attendu {'supprimée' if job.payment.should_unblock else 'présente'}) "
              f"{'✅' if ok_rule else '❌'}")

        # c. paiement UCRM créé
        ucrm_match = [p for p in ucrm_payments if p["clientId"] == s["idclient"]
                      and p["amount"] == s["paid"]]
        ok_ucrm = bool(ucrm_match)
        print(f"  paiement UCRM   : {'créé' if ok_ucrm else 'ABSENT'} {'✅' if ok_ucrm else '❌'}")

        # d. paiement Postgres
        with psycopg.connect(config.DATABASE_URL) as conn:
            cur = conn.execute(
                "SELECT amount, txn_id FROM paiment WHERE idclient=%s AND txn_id=%s",
                (s["idclient"], job.payment.txn_id),
            )
            row = cur.fetchone()
        ok_pg = row is not None and row[0] == s["paid"]
        print(f"  paiement Postgres: {'inséré' if ok_pg else 'ABSENT'} {'✅' if ok_pg else '❌'}")

        # e. message UltraMsg
        client_msg = next(
            (m for m in msgs if s["phone"] in str(m.get("to", ""))),
            None,
        )
        if client_msg:
            body = client_msg.get("body") or client_msg.get("caption", "")
            print(f"  msg WhatsApp    : \"{body}\"")
        else:
            print(f"  msg WhatsApp    : ABSENT ❌")
        print()


if __name__ == "__main__":
    main()
