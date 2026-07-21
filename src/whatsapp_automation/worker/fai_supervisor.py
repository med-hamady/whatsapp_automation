"""Client de l'API blocage/déblocage du superviseur réseau (LR).

Second mécanisme de coupure, à côté du firewall MikroTik (cf. ``mikrotik.py``) :
le blocage est appliqué **directement sur le LR du client** en SSH par le
superviseur, persisté côté superviseur et ré-appliqué toutes les 120 s (il
survit donc à un reboot du LR).

Le système de paiement **ne bloque jamais** : la coupure est décidée ailleurs
(superviseur / équipe réseau). Il ne fait que **lever** la coupure quand un
paiement est validé, et **lire** l'état. La route ``/api/v1/fai/block`` du
superviseur existe mais n'est volontairement pas exposée ici.

    POST /api/v1/fai/unblock  {"mac"}
    GET  /api/v1/fai/status?mac=...

Points d'intégration :

- ``ok: false`` n'est PAS un échec. Il signifie « LR momentanément injoignable,
  l'ordre est enregistré et sera ré-appliqué automatiquement ». C'est
  ``client_blocked`` qui porte l'intention en base. On ne rejoue donc jamais un
  appel : le superviseur s'en charge.
- Le serveur présente un certificat auto-signé → ``verify=False`` (la connexion
  reste chiffrée). Limité à cet hôte, les autres clients HTTP du projet ne sont
  pas touchés.
- La clé d'API est le seul secret qui protège l'accès : elle vient de
  ``FAI_API_KEY`` (.env), jamais du code ni d'une URL.

Sans ``FAI_API_BASE_URL`` + ``FAI_API_KEY`` configurés, le client est
« désactivé » (``enabled()`` → False) et les appels sont des no-op : le projet
tourne alors exactement comme avant l'ajout du superviseur (dev, tests).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from .. import config


logger = logging.getLogger("whatsapp_automation.worker.fai_supervisor")


class FaiSupervisorError(Exception):
    """Erreur d'appel au superviseur.

    ``status_code`` est le code HTTP quand il y en a un (400 MAC mal formée,
    401/403 clé invalide, 404 MAC hors parc supervisé, 409 LR en bridge,
    5xx panne serveur), ``None`` sur une erreur réseau/timeout.
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def enabled() -> bool:
    """True si le superviseur est configuré (URL + clé). Sinon : no-op."""
    return bool(config.FAI_API_BASE_URL and config.FAI_API_KEY)


async def _call(method: str, path: str, timeout: float, **kwargs: Any) -> dict:
    url = f"{config.FAI_API_BASE_URL.rstrip('/')}{path}"
    headers = {"X-API-Key": config.FAI_API_KEY, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            verify=config.FAI_API_VERIFY_SSL,
        ) as client:
            r = await client.request(method, url, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        raise FaiSupervisorError(f"{type(exc).__name__}: {exc}") from exc

    if r.status_code >= 400:
        raise FaiSupervisorError(
            f"HTTP {r.status_code}: {r.text[:200]}", status_code=r.status_code
        )
    try:
        return r.json()
    except ValueError as exc:
        raise FaiSupervisorError(
            f"réponse non-JSON: {r.text[:200]}", status_code=r.status_code
        ) from exc


async def unblock_by_mac(mac: str) -> dict:
    """Rétablit l'accès internet complet du client (quel que soit le mode posé).

    Idempotent : débloquer un client déjà actif renvoie simplement son état.
    Retourne la réponse du superviseur (``ok``, ``message``, ``name``,
    ``client_blocked``, ``retry_scheduled``, ``unenforceable_reason``…).

    Timeout LONG (``FAI_API_TIMEOUT``, ≥ 60 s) : l'appel attend la réponse réelle
    du LR du client. Couper trop tôt ferait croire à un échec alors que l'ordre a
    été exécuté.
    """
    return await _call(
        "POST", "/api/v1/fai/unblock",
        timeout=config.FAI_API_TIMEOUT,
        json={"mac": mac},
    )


async def get_status_by_mac(mac: str) -> dict:
    """État de blocage d'une MAC côté superviseur. Lecture seule (ne touche pas au LR).

    Timeout COURT (``FAI_API_STATUS_TIMEOUT``) : contrairement à unblock, cet
    appel n'attend aucun LR, et il est fait en direct dans /api/clients/lookup —
    un opérateur ne doit pas rester une minute devant une fiche client si le
    superviseur rame.
    """
    return await _call(
        "GET", "/api/v1/fai/status",
        timeout=config.FAI_API_STATUS_TIMEOUT,
        params={"mac": mac},
    )
