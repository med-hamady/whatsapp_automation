"""Fake UltraMsg — simule l'API d'envoi WhatsApp.

Endpoint reproduit :
- POST /{instance}/messages/document   → envoi d'un document (PDF) au client

On stocke les messages envoyés dans MESSAGES pour pouvoir les vérifier en
test. On expose aussi un endpoint GET /fake-pdf/{payment_id} qui retourne
des bytes PDF bidons (sert dans le worker comme source du document).

Lancement : python -m fakes.fake_ultramsg (port 9003).
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

from fastapi import FastAPI, Response
from pydantic import BaseModel


app = FastAPI(title="fake-ultramsg", version="0.1.0")


class DocumentMessage(BaseModel):
    token: str
    to: str
    filename: str
    document: str        # URL du PDF
    caption: str | None = None


MESSAGES: list[dict] = []


@app.post("/{instance}/messages/document")
def send_document(instance: str, msg: DocumentMessage):
    record = {
        "instance": instance,
        "to": msg.to,
        "filename": msg.filename,
        "document": msg.document,
        "caption": msg.caption,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    MESSAGES.append(record)
    return {
        "sent": True,
        "message": f"document queued to {msg.to}",
        "id": len(MESSAGES),
    }


@app.get("/messages")
def list_messages():
    return MESSAGES


@app.get("/fake-pdf/{payment_id}")
def fake_pdf(payment_id: int):
    """Retourne des bytes PDF minimaux valides pour simuler paymentrecue.php."""
    pdf_bytes = (
        b"%PDF-1.4\n"
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n"
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n"
        b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>endobj\n"
        b"xref\n0 4\n0000000000 65535 f\n"
        b"trailer<< /Size 4 /Root 1 0 R >>\nstartxref\n0\n%%EOF\n"
    )
    return Response(content=pdf_bytes, media_type="application/pdf")


@app.get("/health")
def health():
    return {"ok": True, "service": "fake-ultramsg", "messages_count": len(MESSAGES)}


def main():
    import uvicorn

    uvicorn.run(
        "fakes.fake_ultramsg:app",
        host="127.0.0.1",
        port=9003,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
