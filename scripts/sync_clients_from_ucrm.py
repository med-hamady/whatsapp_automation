"""Sync UCRM clients to local postgres `client` table.

Creates the local rows missing for UCRM clients — soit pour des IDs explicites,
soit (``--all``) pour TOUS les clients UCRM absents en local. Utile quand UCRM a
des clients que la DB locale n'a jamais vus (créés via l'UI UCRM, sans passer
par l'ancien flux de provisioning PHP) : sans ligne locale, ces clients sont
invisibles de la recherche par téléphone et n'ont pas de MAC exploitable.

Idempotent : un client déjà présent en local est laissé intact. C'est ce qui
permet de le planifier en tâche récurrente (cf. --all).

Placeholder for `mac` : the local schema has UNIQUE (mac) NOT NULL but UCRM
doesn't always expose a MAC on the service. We insert ``pending-{idclient}``
which keeps the row valid and uniquely identifiable. The worker recognises this
prefix and skips MikroTik unblock until a real MAC is set.

Usage :
    python scripts/sync_clients_from_ucrm.py 1816 1822 1503
    python scripts/sync_clients_from_ucrm.py 1816 --apply
    python scripts/sync_clients_from_ucrm.py --all            # dry-run
    python scripts/sync_clients_from_ucrm.py --all --apply    # tâche planifiée
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from whatsapp_automation import config
from whatsapp_automation.db import postgres as pg


logger = logging.getLogger("sync_clients")


def _crm_headers() -> dict:
    return {"Accept": "application/json", "x-auth-token": config.UCRM_CRM_TOKEN}


async def fetch_all_client_ids(c: httpx.AsyncClient) -> list[int]:
    """Liste les IDs de TOUS les clients UCRM (pagination limit/offset).

    UCRM plafonne le nombre de clients renvoyés par appel ; on boucle jusqu'à
    recevoir une page incomplète.
    """
    base = config.UCRM_BASE_URL
    page = 500
    offset = 0
    ids: list[int] = []
    while True:
        r = await c.get(
            f"{base}/crm/api/v1.0/clients",
            headers=_crm_headers(),
            params={"limit": page, "offset": offset},
        )
        r.raise_for_status()
        lot = r.json() or []
        ids.extend(int(cl["id"]) for cl in lot if cl.get("id") is not None)
        if len(lot) < page:
            return ids
        offset += page


def existing_local_ids() -> set[str]:
    """IDs clients déjà présents en local (comparés en str : idclient est VARCHAR)."""
    with pg.connection() as conn:
        rows = conn.execute("SELECT idclient FROM client").fetchall()
    return {str(row["idclient"]) for row in rows}


async def fetch_client(c: httpx.AsyncClient, cid: int) -> tuple[dict | None, list]:
    base = config.UCRM_BASE_URL
    r = await c.get(f"{base}/crm/api/v1.0/clients/{cid}", headers=_crm_headers())
    if r.status_code != 200:
        return None, []
    client = r.json()
    r2 = await c.get(f"{base}/crm/api/v1.0/clients/services",
                     headers=_crm_headers(), params={"clientId": cid})
    services = r2.json() if r2.status_code == 200 else []
    return client, services


def map_ucrm_to_local(cid: int, client: dict, services: list) -> dict:
    """Construit la ligne `client` locale à partir des objets UCRM."""
    phone = next((ct["phone"] for ct in client.get("contacts", []) if ct.get("phone")), "")
    name = ((client.get("firstName") or "").strip() + " " +
            (client.get("lastName") or "").strip()).strip()
    info = (f"{phone}{name}" if phone else name)[:250] or f"client-{cid}"
    suspended = client.get("hasSuspendedService") or not client.get("isActive", True)
    statu = 2 if suspended else 0
    mac = next((s["macAddress"] for s in services if s.get("macAddress")), "")
    ip = next((s["ip"] for s in services if s.get("ip")), "")
    # Placeholder unique pour respecter NOT NULL + UNIQUE sur `mac`.
    if not mac:
        mac = f"pending-{cid}"
    return {"idclient": str(cid), "info": info, "mac": mac,
            "statu": statu, "ipaddress": ip}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_ids", type=int, nargs="*",
                        help="UCRM client IDs à synchroniser (ignoré si --all)")
    parser.add_argument("--all", action="store_true",
                        help="Synchroniser TOUS les clients UCRM absents en local")
    parser.add_argument("--apply", action="store_true",
                        help="Effectuer les inserts (sinon dry-run)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    if not args.all and not args.client_ids:
        parser.error("fournir des client_ids ou --all")

    async with httpx.AsyncClient(timeout=30.0, verify=False) as c:
        if args.all:
            # On ne va chercher le détail QUE des clients absents en local : la
            # tâche planifiée tourne à vide (0 appel de détail) le reste du temps.
            tous = await fetch_all_client_ids(c)
            connus = existing_local_ids()
            cibles = [cid for cid in tous if str(cid) not in connus]
            logger.info("UCRM=%d clients, local=%d, manquants=%d",
                        len(tous), len(connus), len(cibles))
            if not cibles:
                logger.info("Rien à synchroniser.")
                return 0
        else:
            cibles = args.client_ids

        results = await asyncio.gather(*(fetch_client(c, cid) for cid in cibles))

    rows: list[dict] = []
    for cid, (client, services) in zip(cibles, results):
        if client is None:
            logger.warning("UCRM client %d introuvable, skip", cid)
            continue
        rows.append(map_ucrm_to_local(cid, client, services))

    stats = {"already": 0, "to_insert": 0, "inserted": 0, "errors": 0}
    to_insert: list[dict] = []
    with pg.connection() as conn:
        for row in rows:
            cur = conn.execute("SELECT 1 FROM client WHERE idclient=%s LIMIT 1",
                               (row["idclient"],))
            if cur.fetchone():
                logger.info("cid=%s déjà en DB, skip", row["idclient"])
                stats["already"] += 1
            else:
                to_insert.append(row)
    stats["to_insert"] = len(to_insert)

    for row in to_insert:
        logger.info("  cid=%s  info=%r  statu=%d  mac=%r  ip=%r",
                    row["idclient"], row["info"], row["statu"],
                    row["mac"], row["ipaddress"])

    logger.info("Bilan : déjà_en_db=%d, à_insérer=%d",
                stats["already"], stats["to_insert"])

    if not args.apply:
        logger.info("DRY-RUN : aucun insert. Relance avec --apply.")
        return 0

    for row in to_insert:
        try:
            with pg.connection() as conn:
                conn.execute(
                    """INSERT INTO client (idclient, info, mac, statu, ipaddress)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (row["idclient"], row["info"], row["mac"],
                     row["statu"], row["ipaddress"]),
                )
            stats["inserted"] += 1
        except Exception as exc:
            logger.error("cid=%s insert échoué : %s: %r",
                         row["idclient"], type(exc).__name__, exc)
            stats["errors"] += 1

    logger.info("FAIT : inséré=%d, erreurs=%d",
                stats["inserted"], stats["errors"])
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
