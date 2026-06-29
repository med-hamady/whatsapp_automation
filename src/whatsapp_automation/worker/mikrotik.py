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


async def _http_add_drop_rule(mac: str, comment: str) -> int:
    """Ajoute une règle drop côté fake_mikrotik (dev/tests). Idempotent."""
    existing = await _http_find_rule_ids_by_mac(mac)
    if existing:
        return 0
    url = f"{config.MIKROTIK_BASE_URL}/firewall/rules"
    async with httpx.AsyncClient(timeout=config.MIKROTIK_TIMEOUT) as client:
        r = await client.post(url, json={"mac": mac, "comment": comment, "action": "drop"})
        if r.status_code >= 400:
            raise MikrotikError(f"MikroTik add failed: {r.status_code} {r.text[:200]}")
        return 1


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
        rules = list(api.rawCmd(
            "/ip/firewall/filter/print",
            f"?src-mac-address={mac.upper()}",
            "?action=drop",
        ))
        removed = 0
        for rule in rules:
            rule_id = rule.get(".id")
            if not rule_id:
                continue
            try:
                tuple(api.rawCmd("/ip/firewall/filter/remove", f"=.id={rule_id}"))
                removed += 1
            except Exception as exc:  # pragma: no cover - dépendance réseau
                logger.warning("MikroTik remove .id=%s failed: %s", rule_id, exc)
        return removed
    finally:
        try:
            api.close()
        except Exception:  # pragma: no cover
            pass


def _routeros_block_sync(mac: str, comment: str) -> int:
    """Ajoute une règle firewall DROP pour cette MAC (réplique de add_rules.php).

    Idempotent : si une règle drop existe déjà pour la MAC, on n'en ajoute pas
    de seconde (retourne 0). Sinon on ajoute et retourne 1.

    La règle reproduit exactement celle du PHP :
        /ip/firewall/filter/add chain=forward src-mac-address=<MAC>
        action=drop comment=<...> place-before=0
    ``place-before=0`` insère la règle en tête de la chaîne forward pour qu'elle
    prenne effet avant d'éventuelles règles d'autorisation.
    """
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
        existing = list(api.rawCmd(
            "/ip/firewall/filter/print",
            f"?src-mac-address={mac.upper()}",
            "?action=drop",
        ))
        if existing:
            return 0  # déjà bloqué — pas de doublon
        tuple(api.rawCmd(
            "/ip/firewall/filter/add",
            "=chain=forward",
            f"=src-mac-address={mac.upper()}",
            "=action=drop",
            f"=comment={comment}",
            "=place-before=0",
        ))
        return 1
    finally:
        try:
            api.close()
        except Exception:  # pragma: no cover
            pass


def _routeros_find_rule_ids_sync(mac: str) -> list[str]:
    """Variante lecture-seule de ``_routeros_unblock_sync`` : liste les .id
    des règles drop ciblant cette MAC, sans rien supprimer.
    """
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
        rules = list(api.rawCmd(
            "/ip/firewall/filter/print",
            f"?src-mac-address={mac.upper()}",
            "?action=drop",
        ))
        return [r.get(".id") for r in rules if r.get(".id")]
    finally:
        try:
            api.close()
        except Exception:  # pragma: no cover
            pass


# ----------------------------------------------------------------------------
# API publique
# ----------------------------------------------------------------------------

async def get_block_status_by_mac(mac: str) -> dict:
    """État de blocage d'une MAC sur le routeur (lecture seule).

    Retourne ``{"mac": ..., "is_blocked": bool, "block_rule_count": int}``.
    ``is_blocked`` est vrai dès qu'au moins une règle drop cible la MAC.
    """
    if not mac:
        return {"mac": "", "is_blocked": False, "block_rule_count": 0}
    driver = (config.MIKROTIK_DRIVER or "http").lower()
    if driver == "routeros":
        ids = await asyncio.to_thread(_routeros_find_rule_ids_sync, mac)
    else:
        ids = await _http_find_rule_ids_by_mac(mac)
    return {"mac": mac, "is_blocked": len(ids) > 0, "block_rule_count": len(ids)}


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


async def block_by_mac(mac: str, comment: str = "") -> int:
    """Ajoute une règle firewall DROP pour cette MAC (bloque l'accès réseau).

    Idempotent : retourne 1 si une nouvelle règle a été créée, 0 si la MAC
    était déjà bloquée (aucun doublon ajouté).
    """
    if not mac:
        return 0
    driver = (config.MIKROTIK_DRIVER or "http").lower()
    if driver == "routeros":
        return await asyncio.to_thread(_routeros_block_sync, mac, comment)
    return await _http_add_drop_rule(mac, comment)
