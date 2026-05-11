"""Client MikroTik.

En prod : protocole RouterOS API binaire sur port 8728 (à brancher avec
librouteros). Pour les tests locaux on tape sur le fake_mikrotik (HTTP).
On expose la même interface dans les deux cas.
"""

from __future__ import annotations

import httpx

from .. import config


class MikrotikError(Exception):
    pass


async def find_rule_id_by_mac(mac: str) -> str | None:
    """Equivalent du PHP GetClientIdRouter(mac). Retourne le .id de la règle
    firewall qui bloque ce client, ou None si pas trouvée."""
    url = f"{config.MIKROTIK_BASE_URL}/firewall/find-by-mac/{mac}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
        return data.get("id")


async def remove_rule(rule_id: str) -> bool:
    """Supprime la règle firewall (= déblocage du client)."""
    url = f"{config.MIKROTIK_BASE_URL}/firewall/rules/{rule_id}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.delete(url)
        if r.status_code == 404:
            return False
        if r.status_code >= 400:
            raise MikrotikError(f"MikroTik delete failed: {r.status_code} {r.text[:200]}")
        return True
