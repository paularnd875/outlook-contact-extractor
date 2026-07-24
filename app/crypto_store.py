"""
Chiffrement au repos des extraits d'échanges (objets + corps tronqués) conservés
temporairement pour la pré-classification IA à la demande.

- Chiffrement symétrique Fernet (AES-128-CBC + HMAC).
- La clé est dérivée de SECRET_KEY (aucune clé supplémentaire à gérer).
  Si SECRET_KEY change, les anciens extraits deviennent illisibles (traités
  comme absents) — sans jamais casser l'application.
- Les extraits sont PURGÉS après classification : ce stockage est éphémère par
  conception (données sensibles d'avocats).
"""

import os
import base64
import hashlib
import logging

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet, InvalidToken
    _AVAILABLE = True
except Exception:  # pragma: no cover
    _AVAILABLE = False
    InvalidToken = Exception


def _fernet():
    if not _AVAILABLE:
        return None
    secret = os.getenv("SECRET_KEY", "fallback-secret-key-for-development-only")
    # 32 octets stables dérivés de SECRET_KEY -> clé Fernet urlsafe base64
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt(text: str) -> str:
    """Chiffre une chaîne. Retourne le token (str) ou '' si indisponible/vide."""
    if not text:
        return ""
    f = _fernet()
    if f is None:
        logger.warning("cryptography indisponible : extraits non stockés.")
        return ""
    try:
        return f.encrypt(text.encode("utf-8")).decode("ascii")
    except Exception as e:
        logger.error(f"Chiffrement échoué : {e}")
        return ""


def decrypt(token: str) -> str:
    """Déchiffre un token. Retourne '' si vide/illisible (clé changée, corruption)."""
    if not token:
        return ""
    f = _fernet()
    if f is None:
        return ""
    try:
        return f.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return ""
    except Exception as e:
        logger.error(f"Déchiffrement échoué : {e}")
        return ""
