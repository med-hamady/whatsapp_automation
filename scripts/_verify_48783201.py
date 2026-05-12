import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import httpx
import psycopg

from whatsapp_automation import config

ucrm = httpx.get(
    f"{config.UCRM_BASE_URL}/payments",
    headers={"X-Auth-App-Key": config.UCRM_APP_KEY},
).json()
mine = [p for p in ucrm if p.get("clientId") == 1]
print("=== UCRM payments client 1 ===")
for p in mine:
    method = (p.get("methodId") or "")[:8]
    print(
        f"  id={p['id']} amount={p['amount']} method={method} "
        f"note={p.get('note')!r} user={p.get('userId')}"
    )

mt = httpx.get(f"{config.MIKROTIK_BASE_URL}/firewall/rules").json()
print("\n=== MikroTik rules ===")
for r in mt:
    print(f"  id={r['id']} mac={r['mac_address']} action={r['action']}")
blocked = [r for r in mt if r["mac_address"] == "AA:BB:CC:00:00:01"]
print(f"  -> client 48783201 (MAC ...:01) encore bloque ? {bool(blocked)}")

print("\n=== Postgres client ===")
with psycopg.connect(config.DATABASE_URL) as c:
    cur = c.execute(
        "SELECT idclient, info, statu, mac FROM client WHERE idclient = 1"
    )
    print(f"  {cur.fetchone()}  (statu=0 actif attendu)")

print("=== Postgres paiment ===")
with psycopg.connect(config.DATABASE_URL) as c:
    cur = c.execute(
        "SELECT id_payment, idclient, amount, phone, txn_id "
        "FROM paiment WHERE idclient = 1 ORDER BY id_payment"
    )
    for r in cur.fetchall():
        print(f"  {r}")
