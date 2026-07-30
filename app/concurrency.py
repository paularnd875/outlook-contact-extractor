"""Limiteur de concurrence pour les extractions.

Contexte : l'app tourne sur une petite instance (RAM limitée) avec une base
SQLite (un seul écrivain à la fois). Si plusieurs associés lancent leur
extraction EXACTEMENT en même temps (ex. après un message WhatsApp groupé),
deux problèmes surgissent :
  - la mémoire sature (chaque extraction charge la boîte en RAM) -> risque de
    crash de l'instance, qui ferait échouer TOUTES les extractions en cours ;
  - la contention d'écriture SQLite explose.

Solution : on borne le nombre d'extractions traitées EN PARALLÈLE. Les autres
patientent dans une file d'attente (leur barre de progression affiche « en
attente ») et démarrent automatiquement dès qu'un créneau se libère. Les
extractions étant surtout de l'attente réseau, sérialiser reste rapide et
supprime tout risque de rush.

Réglable via la variable d'environnement MAX_CONCURRENT_EXTRACTIONS
(défaut 2 ; mettre 1 pour une sécurité maximale sur une très petite instance).
"""

import os
import asyncio
import logging

logger = logging.getLogger(__name__)


def _max_concurrent() -> int:
    try:
        return max(1, int(os.getenv("MAX_CONCURRENT_EXTRACTIONS", "2")))
    except (TypeError, ValueError):
        return 2


MAX_CONCURRENT_EXTRACTIONS = _max_concurrent()

# Sémaphore partagé par TOUS les chemins d'extraction (EWS + Microsoft Graph),
# pour que la limite protège l'instance globalement, quel que soit le connecteur.
extraction_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTIONS)

logger.info(f"Limiteur d'extractions initialisé : {MAX_CONCURRENT_EXTRACTIONS} en parallèle max.")


def slots_available() -> bool:
    """True s'il reste un créneau libre (l'extraction démarrera sans attendre)."""
    return not extraction_semaphore.locked()
