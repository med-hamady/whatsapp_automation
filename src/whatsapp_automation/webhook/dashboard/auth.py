"""Authentification simple du dashboard : un seul mot de passe partagé.

Pas de table users, pas de dépendance externe. Après login, on pose un cookie
httponly contenant un jeton signé HMAC-SHA256 (clé = le mot de passe lui-même),
horodaté pour expirer au bout de SESSION_MAX_AGE. Changer le mot de passe
invalide automatiquement toutes les sessions existantes.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import HTTPException, Request

from ... import config

SESSION_COOKIE = "dash_session"
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 jours


def is_enabled() -> bool:
    """Le dashboard n'est actif que si un mot de passe est configuré."""
    return bool(config.DASHBOARD_PASSWORD)


def _key() -> bytes:
    return (config.DASHBOARD_PASSWORD or "").encode("utf-8")


def make_token() -> str:
    issued = str(int(time.time()))
    sig = hmac.new(_key(), issued.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{issued}.{sig}"


def verify_token(token: str) -> bool:
    if not is_enabled() or not token or "." not in token:
        return False
    issued, sig = token.rsplit(".", 1)
    expected = hmac.new(_key(), issued.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        return (time.time() - int(issued)) <= SESSION_MAX_AGE
    except ValueError:
        return False


def check_password(password: str) -> bool:
    if not is_enabled():
        return False
    return hmac.compare_digest(password or "", config.DASHBOARD_PASSWORD)


def is_authenticated(request: Request) -> bool:
    return verify_token(request.cookies.get(SESSION_COOKIE, ""))


def require_session(request: Request) -> None:
    """Dépendance FastAPI : 401 si la session est absente/invalide."""
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="unauthorized")
