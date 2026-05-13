# WhatsApp Automation

Pipeline automatique de paiement pour FAI mauritanien : un client envoie son reçu sur WhatsApp → le système l'OCR, crédite UCRM, débloque MikroTik et renvoie un reçu PDF. 100 % Python, 100 % local pour la partie IA.

## Architecture

```
UltraMsg ──POST──▶ webhook (FastAPI :8010)
                       │
                       ├─ ai_ocr (FastAPI :8008)    ← extraction OCR + IA
                       ├─ PostgreSQL                ← lookup client + insert paiement
                       ├─ UCRM                      ← solde + création paiement
                       └─ queue SQLite              ← idempotence par txn_id
                              │
                              ▼
                       worker(s) async × N
                              │
                              ├─ UCRM POST /payments
                              ├─ MikroTik unblock (si paiement suffisant)
                              ├─ Postgres UPDATE statut
                              └─ UltraMsg send PDF
```

## Structure

```
.
├── README.md
├── CLAUDE.md                      Instructions agent IA
├── pyproject.toml                 Projet Python
├── requirements.txt               Dépendances consolidées
├── .env.example                   Template config (copier en .env)
│
├── src/
│   └── whatsapp_automation/       Package unique
│       ├── config.py
│       ├── ai_ocr/                Service IA (FastAPI :8008)
│       │   ├── service.py
│       │   ├── engine.py
│       │   ├── extractors/
│       │   ├── normalizer.py
│       │   ├── pipeline.py
│       │   ├── dataset/writer.py
│       │   └── annotation/        UI Flask :8009
│       ├── webhook/               Webhook FastAPI :8010
│       ├── worker/                Pool worker
│       ├── jobqueue/              Queue SQLite + idempotence
│       ├── db/                    PostgreSQL clients/paiements
│       └── models/                Pydantic Job/Client/Payment
│
├── scripts/                       Scripts CLI + .bat de lancement
├── data/                          Runtime : queue.db + dataset/store/
└── docs/                          Présentation + plans
```

## Démarrage rapide

```powershell
# Installation
python -m pip install -r requirements.txt

# Configuration
copy .env.example .env

# Lancement (3 fenêtres) — la queue SQLite est créée automatiquement au démarrage
# Aucune initialisation de PostgreSQL : la base prod existe déjà et n'est jamais
# modifiée par ce code (uniquement SELECT/INSERT/UPDATE sur les lignes).
scripts\run_ai_ocr.bat               # :8008 — service IA
scripts\run_webhook.bat              # :8010 — webhook
scripts\run_worker.bat               # worker (relancer N fois pour N workers)
```

## Documentation

- [docs/presentation_migration_python.md](docs/presentation_migration_python.md) — présentation équipe (Marp)
- [docs/plan_implementation_phase2_3.md](docs/plan_implementation_phase2_3.md) — plan d'implémentation
- [CLAUDE.md](CLAUDE.md) — vue d'ensemble pour Claude

## Règle métier

| Cas | Décision |
|---|---|
| Sous-paiement ≤ 150 MRU, paiement exact ou sur-paiement | ✅ Paiement enregistré + client débloqué |
| Sous-paiement > 150 MRU | ⚠ Paiement enregistré, client **non débloqué** |

Seuil configurable via `UNDERPAYMENT_TOLERANCE` dans `.env`.
