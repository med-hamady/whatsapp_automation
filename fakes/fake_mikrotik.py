"""Fake MikroTik — simule l'API RouterOS en HTTP/JSON.

En prod le vrai client utilise librouteros (protocole binaire sur 8728).
Ici on expose les mêmes opérations en HTTP pour faciliter les tests.

Endpoints :
- GET    /firewall/rules                 → liste des règles actuelles
- DELETE /firewall/rules/{rule_id}       → supprime la règle (= unblock client)
- POST   /firewall/rules                 → recrée une règle (pour setup test)

Lancement : python -m fakes.fake_mikrotik (port 9002).
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


app = FastAPI(title="fake-mikrotik", version="0.1.0")


class FirewallRule(BaseModel):
    id: str
    mac_address: str
    src_address: str | None = None         # IP du client suspendu
    action: str = "drop"
    comment: str | None = None


# État simulé des règles firewall (par défaut quelques clients suspendus)
RULES: dict[str, FirewallRule] = {
    "*1A": FirewallRule(id="*1A", mac_address="AA:BB:CC:00:00:01", src_address="10.0.0.1", comment="Suspended"),
    "*2B": FirewallRule(id="*2B", mac_address="AA:BB:CC:00:00:02", src_address="10.0.0.2", comment="Suspended"),
    "*3C": FirewallRule(id="*3C", mac_address="AA:BB:CC:00:00:03", src_address="10.0.0.3", comment="Suspended"),
}


@app.get("/firewall/rules")
def list_rules():
    return list(RULES.values())


@app.get("/firewall/rules/{rule_id}")
def get_rule(rule_id: str):
    if rule_id not in RULES:
        raise HTTPException(status_code=404, detail="rule_not_found")
    return RULES[rule_id]


@app.delete("/firewall/rules/{rule_id}")
def delete_rule(rule_id: str):
    if rule_id not in RULES:
        raise HTTPException(status_code=404, detail="rule_not_found")
    removed = RULES.pop(rule_id)
    return {"ok": True, "removed": removed.model_dump()}


@app.post("/firewall/rules", status_code=201)
def create_rule(rule: FirewallRule):
    """Recrée une règle (utilisé par les scripts de reset/test)."""
    RULES[rule.id] = rule
    return rule


@app.get("/firewall/find-by-mac/{mac}")
def find_by_mac(mac: str):
    """Retourne l'id de la règle qui correspond à cette MAC, ou null."""
    for rule in RULES.values():
        if rule.mac_address.upper() == mac.upper():
            return {"id": rule.id, "mac": rule.mac_address}
    return {"id": None}


@app.get("/firewall/find-by-ip/{ip}")
def find_by_ip(ip: str):
    """Retourne tous les ids des règles qui filtrent cette IP (src_address)."""
    ids = [r.id for r in RULES.values() if r.src_address == ip]
    return {"ids": ids}


@app.get("/health")
def health():
    return {"ok": True, "service": "fake-mikrotik", "rules_count": len(RULES)}


def main():
    import uvicorn

    uvicorn.run(
        "fakes.fake_mikrotik:app",
        host="127.0.0.1",
        port=9002,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
