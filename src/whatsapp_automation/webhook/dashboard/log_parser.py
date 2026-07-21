"""Extraction d'événements métier depuis les logs texte du système.

Source de vérité du dashboard : aucune table d'audit n'existe (choix assumé),
on reconstitue donc les actions à partir des lignes de log déjà écrites par le
webhook (`data/logs/webhook.log*`) et les workers (`data/logs/worker-<pid>.log*`).

Format des lignes (cf. webhook/app.py et worker/main.py) :
    %(asctime)s [%(name)s] %(levelname)s %(message)s
    2026-06-18 15:02:40,909 [whatsapp_automation.worker.handlers] INFO UCRM payment created: ...

Les fichiers `*-stdout.log` / `*-stderr.log` (redirections NSSM) sont ignorés :
ils dupliquent les lignes déjà présentes dans webhook.log / worker-<pid>.log.

Les chaînes de message reconnues sont alignées EXACTEMENT sur les `logger.info`/
`logger.warning` de pipeline.py et handlers.py. Tout changement de formulation
là-bas doit être répercuté dans MESSAGE_PATTERNS ci-dessous (couvert par
scripts/test_dashboard_parser.py).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from whatsapp_automation import config

logger = logging.getLogger("whatsapp_automation.webhook.dashboard")


# --------------------------------------------------------------------------- #
# Types d'événements (valeurs stables : réutilisées par l'API et le front).
# --------------------------------------------------------------------------- #
REFUSED = "refused"                       # paiement refusé (toutes causes)
CLIENT_NOT_FOUND = "client_not_found"     # numéro non rattaché à un client (section dédiée)
RECIPIENT_SUSPECT = "recipient_suspect"   # destinataire suspect (observation, non bloquant)
PAYMENT_ENQUEUED = "payment_enqueued"     # reçu validé → empilé en queue
UCRM_CREATED = "ucrm_created"             # paiement créé dans le CRM (UCRM)
CLIENT_UNBLOCKED = "client_unblocked"     # déblocage MikroTik (par le worker)
SUBSCRIPTION_ACTIVATED = "subscription_activated"  # statut abo → actif
UNDERPAYMENT = "underpayment"             # sous-paiement RÉEL : écart > tolérance
PAYMENT_COMPLETE = "payment_complete"     # paiement complet (écart ≤ tolérance) : le
                                          # worker loguait "sous-paiement" mais le
                                          # compte était à jour (mislabel historique,
                                          # cf. bug MAC UCRM absent / client déjà actif)
MESSAGE_SENT = "message_sent"             # reçu PDF envoyé au client
SUPPORT_NOTIFIED = "support_notified"     # notification envoyée au support


# Causes de "refus" exclues des agrégations du dashboard : ce ne sont pas de
# vrais refus de paiement (message hors-sujet ou cas métier normal), et ils
# écrasent numériquement les vraies causes. On continue de les parser, mais on
# ne les compte ni n'affiche dans les vues "refus".
#   - unsupported_type     : message non-image (vidéo, sticker, audio, PDF).
#   - subscription_form    : fiche "Nouvel abonnement" Connect A2 (pas un reçu).
#   - client_not_suspended : ancien rejet (client déjà actif) ; plus émis
#                            aujourd'hui (validate_client accepte tous statuts).
#   - no_or_invalid_amount : OCR sans montant exploitable (image illisible /
#                            non-reçu) ; pas un refus de paiement réel.
EXCLUDED_REFUSAL_REASONS = {
    "unsupported_type", "subscription_form", "client_not_suspended", "no_or_invalid_amount",
}


def _is_counted_refusal(e: "Event") -> bool:
    return e.type == REFUSED and e.reason not in EXCLUDED_REFUSAL_REASONS


@dataclass
class Event:
    ts: datetime
    type: str
    reason: Optional[str] = None
    client_id: Optional[int] = None
    phone: Optional[str] = None
    txn_id: Optional[str] = None
    amount: Optional[int] = None
    balance: Optional[int] = None
    mac: Optional[str] = None
    operator: Optional[str] = None
    payment_id: Optional[str] = None
    raw: str = ""

    def to_dict(self) -> dict:
        return {
            "ts": self.ts.strftime("%Y-%m-%d %H:%M:%S"),
            "date": self.ts.strftime("%Y-%m-%d"),
            "type": self.type,
            "reason": self.reason,
            "client_id": self.client_id,
            "phone": self.phone,
            "txn_id": self.txn_id,
            "amount": self.amount,
            "balance": self.balance,
            "mac": self.mac,
            "operator": self.operator,
            "payment_id": self.payment_id,
        }


# Préfixe commun : `2026-06-18 15:02:40,909 [logger] LEVEL <message>`
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} "
    r"\[(?P<logger>[^\]]+)\] (?P<level>\w+) (?P<msg>.*)$"
)


def _ev(**kw) -> dict:
    return kw


def _pid_from_url(url: Optional[str]) -> Optional[str]:
    """Extrait le payment_id (dernier groupe de chiffres) d'une URL de reçu PDF,
    ex : http://.../paymentrecue.php?id=22359 → '22359'."""
    if not url:
        return None
    m = re.search(r"(\d+)\D*$", url)
    return m.group(1) if m else None


# Liste ordonnée (regex compilée, builder). Le 1er motif qui matche gagne ; les
# motifs les plus spécifiques d'abord. `m` est le match du MESSAGE (pas la ligne
# entière). Le builder renvoie les champs de l'Event (hors ts/raw).
MESSAGE_PATTERNS: list[tuple[re.Pattern, Callable[[re.Match], dict]]] = [
    # ---- Refus (logger ...webhook.pipeline) ----
    (re.compile(r"^type=(?P<mtype>\S+) non support\w*, drop"),
     lambda m: _ev(type=REFUSED, reason="unsupported_type")),
    (re.compile(r"^no media, drop"),
     lambda m: _ev(type=REFUSED, reason="no_media")),
    (re.compile(r"^document non-paiement \w+: (?P<reason>\S+)"),
     lambda m: _ev(type=REFUSED, reason=m.group("reason"))),
    (re.compile(r"^extraction invalide: (?P<reason>\S+)"),
     lambda m: _ev(type=REFUSED, reason=m.group("reason"))),
    # client_not_found a son PROPRE type (section "Clients introuvables"),
    # hors des paiements refusés. Doit précéder le motif générique ci-dessous.
    (re.compile(r"^validation client KO: client_not_found \(phone=(?P<phone>[^ )]*)"),
     lambda m: _ev(type=CLIENT_NOT_FOUND, reason="client_not_found", phone=m.group("phone") or None)),
    (re.compile(r"^validation client KO: (?P<reason>\S+) \(phone=(?P<phone>[^ )]*)"),
     lambda m: _ev(type=REFUSED, reason=m.group("reason"), phone=m.group("phone") or None)),
    (re.compile(r"^UCRM injoignable \(client=(?P<client>\d+)\)"),
     lambda m: _ev(type=REFUSED, reason="crm_unreachable", client_id=int(m.group("client")))),
    (re.compile(r"^paiement refus\w* : (?P<reason>\S+) \(client=(?P<client>\d+) "
                r"balance=(?P<bal>-?\d+) pay\w*=(?P<paid>\d+) txn=(?P<txn>\S*)\)"),
     lambda m: _ev(type=REFUSED, reason=m.group("reason"), client_id=int(m.group("client")),
                   amount=int(m.group("paid")), balance=int(m.group("bal")),
                   txn_id=m.group("txn") or None)),
    (re.compile(r"^idempotence: txn_id (?P<txn>\S+) d\w+ trait\w* avec succ"),
     lambda m: _ev(type=REFUSED, reason="duplicate_processed", txn_id=m.group("txn"))),
    (re.compile(r"^idempotence: txn_id (?P<txn>\S+) d\w+ en queue"),
     lambda m: _ev(type=REFUSED, reason="duplicate_in_flight", txn_id=m.group("txn"))),
    (re.compile(r"^idempotence atomique: txn_id (?P<txn>\S+) "),
     lambda m: _ev(type=REFUSED, reason="duplicate_race", txn_id=m.group("txn"))),
    # ---- Observation (non bloquant) ----
    (re.compile(r"^destinataire suspect \(PASS-THROUGH\) : (?P<reason>\S+)"),
     lambda m: _ev(type=RECIPIENT_SUSPECT, reason=m.group("reason"))),
    # ---- Succès / actions (pipeline) ----
    (re.compile(r"^job enqueued id=(?P<iid>\d+) job_id=(?P<jid>\S+) client=(?P<client>\d+) "
                r"amount=(?P<amt>\d+) txn=(?P<txn>\S*)"),
     lambda m: _ev(type=PAYMENT_ENQUEUED, client_id=int(m.group("client")),
                   amount=int(m.group("amt")), txn_id=m.group("txn") or None)),
    (re.compile(r"^support notifi\w* reason=(?P<reason>\S+)"),
     lambda m: _ev(type=SUPPORT_NOTIFIED, reason=m.group("reason"))),
    # ---- Actions (worker.handlers) ----
    (re.compile(r"^UCRM payment created: client=(?P<client>\d+) amount=(?P<amt>\d+) "
                r"paymentId=(?P<pid>\S+) operator=(?P<op>\S+) txn=(?P<txn>\S*)"),
     lambda m: _ev(type=UCRM_CREATED, client_id=int(m.group("client")), amount=int(m.group("amt")),
                   payment_id=m.group("pid"), operator=m.group("op"), txn_id=m.group("txn") or None)),
    (re.compile(r"^MikroTik unblock: client=(?P<client>\d+) mac=(?P<mac>\S+) rules_removed=(?P<n>\d+)"),
     lambda m: _ev(type=CLIENT_UNBLOCKED, client_id=int(m.group("client")), mac=m.group("mac"))),
    (re.compile(r"^Statut abo mac=(?P<mac>\S+) \S+ actif \(lignes=(?P<n>\d+), client=(?P<client>\d+)\)"),
     lambda m: _ev(type=SUBSCRIPTION_ACTIVATED, mac=m.group("mac"), client_id=int(m.group("client")))),
    # Le worker logue "sous-paiement" dès que should_unblock=False. Ce n'est un
    # VRAI sous-paiement que si l'écart (dû - payé) dépasse la tolérance ; en
    # deçà, le compte est à jour (mislabel) → on classe en PAYMENT_COMPLETE.
    (re.compile(r"^sous-paiement \(balance=(?P<bal>\d+) pay\w*=(?P<paid>\d+) \S+=(?P<ecart>-?\d+)\) "
                r".*client=(?P<client>\d+)\)"),
     lambda m: _ev(
         type=(UNDERPAYMENT if int(m.group("ecart")) > config.UNDERPAYMENT_TOLERANCE
               else PAYMENT_COMPLETE),
         client_id=int(m.group("client")), amount=int(m.group("paid")),
         balance=int(m.group("bal")))),
    (re.compile(r"^PDF envoy\w* via UltraMsg \S+ \+222(?P<phone>\S+) \(unblocked=(?P<ub>\w+)\)"
                r"(?: url=(?P<url>\S+))?"),
     lambda m: _ev(type=MESSAGE_SENT, phone=m.group("phone"),
                   payment_id=_pid_from_url(m.group("url")))),
    # Note : les blocages/déblocages MANUELS (block_client OK ...) ne sont
    # volontairement PAS parsés — hors périmètre de cette interface.
]


def _default_log_dir() -> str:
    # Aligné sur webhook/app.py : les logs sont écrits dans <cwd>/data/logs.
    # Webhook et dashboard tournent dans le même process → même cwd.
    return os.path.join(os.getcwd(), "data", "logs")


# Fichiers à parser : webhook.log(.N) et worker-<pid>.log(.N). On exclut
# explicitement les redirections NSSM (*-stdout.log / *-stderr.log).
_FILE_RE = re.compile(r"^(webhook|worker-\d+)\.log(\.\d+)?$")


def _log_files(log_dir: str) -> list[str]:
    try:
        names = os.listdir(log_dir)
    except OSError:
        return []
    return [os.path.join(log_dir, n) for n in names if _FILE_RE.match(n)]


def _parse_message(msg: str) -> Optional[dict]:
    for pattern, builder in MESSAGE_PATTERNS:
        m = pattern.match(msg)
        if m:
            return builder(m)
    return None


def _parse_file(path: str) -> list[Event]:
    events: list[Event] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                lm = _LINE_RE.match(line.rstrip("\n"))
                if not lm:
                    continue  # ligne de continuation (traceback) ou format inconnu
                fields = _parse_message(lm.group("msg"))
                if fields is None:
                    continue
                try:
                    ts = datetime.strptime(lm.group("ts"), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                events.append(Event(ts=ts, raw=lm.group("msg"), **fields))
    except OSError as exc:
        logger.warning("dashboard: lecture log %s impossible: %s", path, exc)
    return events


# --------------------------------------------------------------------------- #
# Cache mémoire : re-parse complet au plus une fois toutes les _TTL secondes.
# Suffisant pour un dashboard interne ; un parse de ~50 Mo prend ~1-2 s.
# --------------------------------------------------------------------------- #
_TTL = 30.0
_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "events": []}


def get_events(log_dir: Optional[str] = None, force: bool = False) -> list[Event]:
    """Renvoie tous les événements parsés (triés par timestamp croissant)."""
    if log_dir is not None:
        # Mode test / chemin explicite : pas de cache global.
        return _load(log_dir)
    now = time.time()
    with _lock:
        if not force and _cache["events"] and (now - _cache["ts"]) < _TTL:
            return _cache["events"]
        events = _load(_default_log_dir())
        _cache["events"] = events
        _cache["ts"] = now
        return events


def _load(log_dir: str) -> list[Event]:
    events: list[Event] = []
    for path in _log_files(log_dir):
        events.extend(_parse_file(path))
    events.sort(key=lambda e: e.ts)
    return events


def _within(events: list[Event], days: Optional[int]) -> list[Event]:
    if not days:
        return events
    since = datetime.now() - timedelta(days=days)
    return [e for e in events if e.ts >= since]


# --------------------------------------------------------------------------- #
# Agrégations exposées à l'API.
# --------------------------------------------------------------------------- #
def summary(days: Optional[int] = 30, log_dir: Optional[str] = None) -> dict:
    """Compteurs KPI sur la période (jours). Toutes les valeurs viennent des logs."""
    events = _within(get_events(log_dir), days)
    by_type: Counter = Counter(e.type for e in events)
    refused = [e for e in events if _is_counted_refusal(e)]
    return {
        "period_days": days,
        "payments_enqueued": by_type.get(PAYMENT_ENQUEUED, 0),
        "ucrm_created": by_type.get(UCRM_CREATED, 0),
        "messages_sent": by_type.get(MESSAGE_SENT, 0),
        "clients_unblocked": by_type.get(CLIENT_UNBLOCKED, 0),
        "clients_unblocked_distinct": len({e.client_id for e in events
                                           if e.type == CLIENT_UNBLOCKED and e.client_id}),
        "subscriptions_activated": by_type.get(SUBSCRIPTION_ACTIVATED, 0),
        "underpayments": by_type.get(UNDERPAYMENT, 0),
        "refused_total": len(refused),
        "clients_not_found": by_type.get(CLIENT_NOT_FOUND, 0),
        "support_notified": by_type.get(SUPPORT_NOTIFIED, 0),
        "recipient_suspect": by_type.get(RECIPIENT_SUSPECT, 0),
    }


def refusals_by_cause(days: Optional[int] = 30, log_dir: Optional[str] = None) -> dict:
    """Répartition {cause: count} des paiements refusés sur la période."""
    events = _within(get_events(log_dir), days)
    counter: Counter = Counter(
        (e.reason or "inconnu") for e in events if _is_counted_refusal(e)
    )
    return dict(counter.most_common())


def timeseries(days: int = 30, log_dir: Optional[str] = None) -> dict:
    """Séries journalières : créés CRM, refusés, messages envoyés, déblocages."""
    events = _within(get_events(log_dir), days)
    buckets: dict[str, Counter] = defaultdict(Counter)
    for e in events:
        if e.type == REFUSED and not _is_counted_refusal(e):
            continue  # exclut unsupported_type de la série "refusés"
        day = e.ts.strftime("%Y-%m-%d")
        buckets[day][e.type] += 1
    labels = sorted(buckets.keys())
    return {
        "labels": labels,
        "ucrm_created": [buckets[d].get(UCRM_CREATED, 0) for d in labels],
        "refused": [buckets[d].get(REFUSED, 0) for d in labels],
        "messages_sent": [buckets[d].get(MESSAGE_SENT, 0) for d in labels],
        "clients_unblocked": [buckets[d].get(CLIENT_UNBLOCKED, 0) for d in labels],
    }


def recent_events(
    limit: int = 100,
    type_filter: Optional[str] = None,
    days: Optional[int] = 30,
    log_dir: Optional[str] = None,
) -> list[dict]:
    """Derniers événements (les plus récents d'abord), filtrables par type."""
    events = _within(get_events(log_dir), days)
    # Cache les messages non-paiement exclus (unsupported_type) de la table.
    events = [e for e in events
              if not (e.type == REFUSED and not _is_counted_refusal(e))]
    if type_filter:
        events = [e for e in events if e.type == type_filter]
    events = sorted(events, key=lambda e: e.ts, reverse=True)[:limit]
    return [e.to_dict() for e in events]
