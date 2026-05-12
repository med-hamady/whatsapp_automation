"""Client MikroTik.

Deux drivers cohabitent, sélectionnés par ``MIKROTIK_DRIVER`` :

- ``"http"``     : tape sur fake_mikrotik (port 9002) — dev / tests.
- ``"routeros"`` : protocole binaire RouterOS via librouteros — PROD.

Le déblocage repose sur la **MAC** du client (clé canonique côté prod, cf.
``admin.php::GetClientIdRouter``) : on liste les règles firewall qui
filtrent cette MAC avec ``action=drop``, puis on les supprime.

L'interface publique reste asynchrone (``async def unblock_by_mac``) ;
côté routeros le client librouteros est synchrone donc on l'exécute via
``asyncio.to_thread`` pour ne pas bloquer la loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .. import config


logger = logging.getLogger("whatsapp_automation.worker.mikrotik")


class MikrotikError(Exception):
    pass


# ----------------------------------------------------------------------------
# Driver HTTP (fake_mikrotik)
# ----------------------------------------------------------------------------

async def _http_find_rule_ids_by_mac(mac: str) -> list[str]:
    url = f"{config.MIKROTIK_BASE_URL}/firewall/find-by-mac/{mac}"
    async with httpx.AsyncClient(timeout=config.MIKROTIK_TIMEOUT) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
        ids = data.get("ids")
        if ids is not None:
            return list(ids)
        single = data.get("id")
        return [single] if single else []


async def _http_remove_rule(rule_id: str) -> bool:
    url = f"{config.MIKROTIK_BASE_URL}/firewall/rules/{rule_id}"
    async with httpx.AsyncClient(timeout=config.MIKROTIK_TIMEOUT) as client:
        r = await client.delete(url)
        if r.status_code == 404:
            return False
        if r.status_code >= 400:
            raise MikrotikError(f"MikroTik delete failed: {r.status_code} {r.text[:200]}")
        return True


# ----------------------------------------------------------------------------
# Driver RouterOS binaire (librouteros) — PROD
# ----------------------------------------------------------------------------
#
# Réplique de ``admin.php::GetClientIdRouter`` :
#
#     /ip/firewall/filter/print
#     ?src-mac-address=<MAC en majuscules>
#     ?action=drop
#
# Suivi de la suppression :
#
#     /ip/firewall/filter/remove
#     =.id=<.id récupéré>

def _routeros_unblock_sync(mac: str) -> int:
    """Bloc synchrone exécuté dans un thread (cf. ``unblock_by_mac``).

    Retourne le nombre de règles supprimées.
    """
    # Import paresseux : librouteros n'est nécessaire qu'en prod, et son
    # absence ne doit pas casser les tests qui utilisent le driver HTTP.
    try:
        from librouteros import connect as ros_connect
    except ImportError as exc:  # pragma: no cover - dépendance prod
        raise MikrotikError(
            "librouteros n'est pas installé — `pip install librouteros` "
            "ou repasser MIKROTIK_DRIVER=http"
        ) from exc

    api = ros_connect(
        username=config.MIKROTIK_USER,
        password=config.MIKROTIK_PASSWORD,
        host=config.MIKROTIK_HOST,
        port=config.MIKROTIK_PORT,
        timeout=config.MIKROTIK_TIMEOUT,
    )
    try:
        rules = list(api(
            cmd="/ip/firewall/filter/print",
            **{
                "?src-mac-address": mac.upper(),
                "?action": "drop",
            },
        ))
        removed = 0
        for rule in rules:
            rule_id = rule.get(".id")
            if not rule_id:
                continue
            try:
                tuple(api(cmd="/ip/firewall/filter/remove", **{"=.id": rule_id}))
                removed += 1
            except Exception as exc:  # pragma: no cover - dépendance réseau
                logger.warning("MikroTik remove .id=%s failed: %s", rule_id, exc)
        return removed
    finally:
        try:
            api.close()
        except Exception:  # pragma: no cover
            pass


# ----------------------------------------------------------------------------
# API publique
# ----------------------------------------------------------------------------

async def unblock_by_mac(mac: str) -> int:
    """Supprime toutes les règles firewall qui bloquent cette MAC.

    Retourne le nombre de règles effectivement supprimées (0 si aucune ;
    ce n'est pas une erreur — le client n'était peut-être déjà plus bloqué).
    """
    if not mac:
        return 0
    driver = (config.MIKROTIK_DRIVER or "http").lower()
    if driver == "routeros":
        return await asyncio.to_thread(_routeros_unblock_sync, mac)

    ids = await _http_find_rule_ids_by_mac(mac)
    removed = 0
    for rid in ids:
        if await _http_remove_rule(rid):
            removed += 1
    return removed
