"""Dashboard de supervision (lecture seule) du système de paiement.

Expose un `router` FastAPI monté par le service webhook. Les données affichées
sont extraites des logs texte existants (cf. log_parser) + complétées par la
queue SQLite et la table `paiment`. Accès protégé par mot de passe simple.
"""

from .routes import router

__all__ = ["router"]
