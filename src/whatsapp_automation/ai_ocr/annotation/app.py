"""UI Flask d'annotation des reçus collectés.

Permet à un opérateur de :
  - Lister les échantillons en attente d'annotation.
  - Voir l'image + la prédiction du modèle.
  - Corriger (ou valider) les valeurs et écrire label.json.

Écoute sur 127.0.0.1:8009 (jamais exposé hors machine).

Sécurité :
- ``sample_id`` validé par ``dataset/writer.sample_dir`` (refus path traversal).
- Token CSRF généré au démarrage, requis sur tous les POST.
- ``Host:`` strictement contrôlé (defense en profondeur contre DNS rebinding).
- Le mimetype de ``/image/...`` est dérivé du contenu réel.
- Liste paginée (50 samples par page) pour ne pas saturer la RAM si beaucoup
  d'échantillons.
"""

from __future__ import annotations

import secrets
from itertools import islice
from pathlib import Path

from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from ..dataset.writer import iter_samples, load_sample, sample_dir, write_label
from ..normalizer import normalize_amount, normalize_datetime, normalize_txn_id


app = Flask(__name__)

# Token CSRF généré à chaque démarrage du process. Stocké en config Flask
# (visible par les templates via {{ csrf_token }}).
_CSRF_TOKEN = secrets.token_urlsafe(32)
app.config["CSRF_TOKEN"] = _CSRF_TOKEN

_ALLOWED_HOSTS = {"127.0.0.1:8009", "localhost:8009"}
_PAGE_SIZE = 50


@app.before_request
def _check_host_and_csrf():
    """Refuse les Host: non locaux (anti DNS rebinding) et exige le token
    CSRF sur les POST."""
    host = request.headers.get("Host", "")
    if host not in _ALLOWED_HOSTS:
        abort(400, "invalid host")
    if request.method == "POST":
        token = request.form.get("csrf_token", "")
        if not secrets.compare_digest(token, _CSRF_TOKEN):
            abort(403, "csrf token invalid")


@app.context_processor
def _inject_csrf():
    return {"csrf_token": _CSRF_TOKEN}


@app.route("/")
def index():
    only_unlabeled = request.args.get("show", "todo") == "todo"
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    start = (page - 1) * _PAGE_SIZE
    samples = list(islice(iter_samples(only_unlabeled=only_unlabeled), start, start + _PAGE_SIZE))
    has_next = len(samples) == _PAGE_SIZE
    return render_template(
        "list.html",
        samples=samples,
        only_unlabeled=only_unlabeled,
        page=page,
        has_next=has_next,
    )


@app.route("/sample/<path:sample_id>", methods=["GET", "POST"])
def sample_view(sample_id: str):
    try:
        sample_dir(sample_id)
    except ValueError:
        abort(400, "invalid sample id")

    data = load_sample(sample_id)
    if data is None:
        abort(404)

    if request.method == "POST":
        label = {
            "montant": normalize_amount(request.form.get("montant")),
            "txn_id": normalize_txn_id(request.form.get("txn_id")),
            "date_heure": _accept_iso_or_normalize(request.form.get("date_heure")),
            "operator": request.form.get("operator", "").strip() or None,
            "valid": request.form.get("valid") == "on",
            "notes": request.form.get("notes", "").strip() or None,
        }
        write_label(sample_id, label)
        next_id = _next_unlabeled_after(sample_id)
        if next_id:
            return redirect(url_for("sample_view", sample_id=next_id))
        return redirect(url_for("index"))

    return render_template("edit.html", **data)


@app.route("/image/<path:sample_id>")
def sample_image(sample_id: str):
    try:
        sample_dir(sample_id)
    except ValueError:
        abort(400, "invalid sample id")

    data = load_sample(sample_id)
    if data is None:
        abort(404)
    image_path = Path(data["image_path"])
    if not image_path.exists():
        abort(404)
    # Détecter le format réel : save_sample écrit toujours en .jpg mais les
    # bytes peuvent être PNG/WEBP. Sniff les magic bytes.
    mimetype = _sniff_image_mimetype(image_path)
    return send_file(image_path, mimetype=mimetype)


def _sniff_image_mimetype(path: Path) -> str:
    try:
        with path.open("rb") as f:
            head = f.read(12)
    except OSError:
        return "application/octet-stream"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:2] == b"BM":
        return "image/bmp"
    return "image/jpeg"


def _accept_iso_or_normalize(raw):
    if raw is None or not raw.strip():
        return None
    raw = raw.strip()
    try:
        from datetime import datetime

        datetime.fromisoformat(raw)
        return raw
    except ValueError:
        return normalize_datetime(raw)


def _next_unlabeled_after(current_id: str):
    found_current = False
    for sample in iter_samples(only_unlabeled=True):
        if found_current:
            return sample["sample_id"]
        if sample["sample_id"] == current_id:
            found_current = True
    for sample in iter_samples(only_unlabeled=True):
        if sample["sample_id"] != current_id:
            return sample["sample_id"]
    return None


def main():
    app.run(host="127.0.0.1", port=8009, debug=False)


if __name__ == "__main__":
    main()
