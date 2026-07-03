"""
Diagnostic à distance : capture les logs en mémoire et les expose (avec l'état des
extractions) via un endpoint protégé par une clé, pour déboguer une instance déployée
sans accès aux logs de l'hébergeur.
"""

import os
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
async def diag(key: str = Query(...), db: AsyncSession = Depends(get_db)):
    """État des dernières extractions + derniers logs. Protégé par la clé (= AZURE_CLIENT_ID)."""
    if key != os.getenv("AZURE_CLIENT_ID"):
        raise HTTPException(status_code=403, detail="clé invalide")

    result = await db.execute(
        select(ExtractionSession).order_by(desc(ExtractionSession.date_debut)).limit(8)
    )
    sessions = result.scalars().all()
    out = []
    for s in sessions:
        cnt = (await db.execute(
            select(func.count(Contact.id)).where(Contact.session_id == s.id)
        )).scalar()
        out.append({
            "id": s.id,
            "status": s.status,
            "date_debut": s.date_debut.isoformat() if s.date_debut else None,
            "date_fin": s.date_fin.isoformat() if s.date_fin else None,
            "total_emails": s.total_emails,
            "contacts_reels": cnt,
            "current_step": s.current_step,
            "erreur": s.erreur_message,
        })

    return {"sessions": out, "logs": list(_LOG_BUFFER)[-300:]}
