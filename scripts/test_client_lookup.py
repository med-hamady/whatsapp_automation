"""Smoke test pour l'endpoint /api/clients/lookup.

Vérifie :
  1. 401 sans header X-API-Key
  2. 401 avec mauvaise clé
  3. 200 + found=false sur téléphone inconnu
  4. 200 + structure complète sur téléphone connu (si fourni en argv)

Usage :
    set CLIENT_API_KEY=test-key-123   (côté serveur + côté script)
    set BASE_URL=http://localhost:8000
    python scripts/test_client_lookup.py [phone_connu]

L'endpoint doit être servi par uvicorn (cf. webhook/app.py).
"""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlencode

import httpx


def _check(label: str, cond: bool, detail: str = "") -> None:
    status = "OK " if cond else "FAIL"
    print(f"  [{status}] {label}" + (f"  -- {detail}" if detail else ""))
    if not cond:
        sys.exit(1)


def main() -> None:
    base = os.environ.get("BASE_URL", "http://localhost:8000")
    api_key = os.environ.get("CLIENT_API_KEY", "")
    if not api_key:
        print("CLIENT_API_KEY non défini dans l'environnement — skip", file=sys.stderr)
        sys.exit(2)

    known_phone = sys.argv[1] if len(sys.argv) > 1 else None
    unknown_phone = "00000000"

    print(f"Cible : {base}")

    # 1) Sans header → 401
    r = httpx.get(f"{base}/api/clients/lookup", params={"phone": unknown_phone}, timeout=10)
    _check("sans header → 401", r.status_code == 401, f"got {r.status_code}")

    # 2) Mauvaise clé → 401
    r = httpx.get(
        f"{base}/api/clients/lookup",
        params={"phone": unknown_phone},
        headers={"X-API-Key": "wrong-key"},
        timeout=10,
    )
    _check("mauvaise clé → 401", r.status_code == 401, f"got {r.status_code}")

    # 3) Téléphone inconnu → 200 + found=false
    r = httpx.get(
        f"{base}/api/clients/lookup",
        params={"phone": unknown_phone},
        headers={"X-API-Key": api_key},
        timeout=10,
    )
    _check("inconnu → 200", r.status_code == 200, f"got {r.status_code}")
    body = r.json()
    _check("inconnu → found=False", body.get("found") is False, json.dumps(body))
    _check("inconnu → crm=None", body.get("crm") is None)
    _check("inconnu → fai=None", body.get("fai") is None)

    # 4) Téléphone connu (si fourni en argv)
    if known_phone:
        r = httpx.get(
            f"{base}/api/clients/lookup",
            params={"phone": known_phone},
            headers={"X-API-Key": api_key},
            timeout=30,
        )
        _check("connu → 200", r.status_code == 200, f"got {r.status_code}")
        body = r.json()
        _check("connu → found=True", body.get("found") is True, json.dumps(body)[:300])
        _check("connu → errors présent", "errors" in body)
        # CRM/FAI peuvent être null si services injoignables — c'est OK,
        # on vérifie juste que les clés existent.
        _check("connu → clé crm présente", "crm" in body)
        _check("connu → clé fai présente", "fai" in body)
        print("\n--- Réponse complète pour", known_phone, "---")
        print(json.dumps(body, indent=2, ensure_ascii=False)[:2000])
    else:
        print("\n(astuce) Passe un numéro connu en argv pour tester le cas nominal :")
        print("        python scripts/test_client_lookup.py 37697850")

    print("\nTous les checks OK.")


if __name__ == "__main__":
    main()
