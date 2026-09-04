"""
Diagnostic à distance : capture les logs en mémoire et les expose (avec l'état des
extractions) via un endpoint protégé par une clé, pour déboguer une instance déployée
sans accès aux logs de l'hébergeur.
"""

import os
import time
import socket
import asyncio
import logging
import collections

from fastapi import APIRouter, Query, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from app.database import get_db, ExtractionSession, Contact

# Tampon circulaire des derniers logs
_LOG_BUFFER = collections.deque(maxlen=500)


class _BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            _LOG_BUFFER.append(self.format(record))
        except Exception:
            pass


def setup_log_capture():
    """Attache un handler qui garde les derniers logs en mémoire."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, _BufferHandler) for h in root.handlers):
        h = _BufferHandler()
        h.setLevel(logging.INFO)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(h)
    logging.getLogger("app").setLevel(logging.INFO)


diag_router = APIRouter()


@diag_router.get("/diag")
async def diag(key: str = Query(...), limit: int = Query(50, ge=1, le=500),
               db: AsyncSession = Depends(get_db)):
    """État des dernières extractions + derniers logs. Protégé par la clé (= AZURE_CLIENT_ID)."""
    if key != os.getenv("AZURE_CLIENT_ID"):
        raise HTTPException(status_code=403, detail="clé invalide")

    result = await db.execute(
        select(ExtractionSession).order_by(desc(ExtractionSession.date_debut)).limit(limit)
    )
    sessions = result.scalars().all()
    out = []
    for s in sessions:
        cnt = (await db.execute(
            select(func.count(Contact.id)).where(Contact.session_id == s.id)
        )).scalar()
        # Type de connecteur déduit du user_id (ews:... pour Exchange hébergé, sinon Graph/M365)
        connecteur = "ews" if (s.user_id or "").startswith("ews:") else "graph"
        out.append({
            "id": s.id,
            "email_address": s.email_address,
            "owner_name": s.owner_name,
            "user_id": s.user_id,
            "connecteur": connecteur,
            "status": s.status,
            "date_debut": s.date_debut.isoformat() if s.date_debut else None,
            "date_fin": s.date_fin.isoformat() if s.date_fin else None,
            "total_emails": s.total_emails,
            "contacts_reels": cnt,
            "current_step": s.current_step,
            "erreur": s.erreur_message,
        })

    return {"sessions": out, "logs": list(_LOG_BUFFER)[-300:]}


@diag_router.get("/diag/ews-reachable")
async def ews_reachable(key: str = Query(...),
                        host: str = Query("webmail.cloudexchange.fr"),
                        port: int = Query(443, ge=1, le=65535),
                        timeout: float = Query(15.0, gt=0, le=120)):
    """Sonde SANS identifiants : depuis l'IP de Render, tente un simple connect TCP
    vers le serveur Exchange hébergé. Sert à savoir si l'IP de Render est bannie par
    le pare-feu de l'hébergeur (timeout = droppée ; ok = débloquée) sans avoir besoin
    du mot de passe d'un associé. Protégé par la clé (= AZURE_CLIENT_ID)."""
    if key != os.getenv("AZURE_CLIENT_ID"):
        raise HTTPException(status_code=403, detail="clé invalide")

    def _probe():
        t0 = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return {"reachable": True, "ms": round((time.monotonic() - t0) * 1000)}
        except Exception as e:
            return {"reachable": False, "ms": round((time.monotonic() - t0) * 1000),
                    "error": f"{type(e).__name__}: {e}"}

    result = await asyncio.to_thread(_probe)
    return {"host": host, "port": port, **result}
