"""
API pour l'extraction via Exchange hébergé (EWS) — hors Microsoft 365.
"""

import uuid
import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException, BackgroundTasks, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, SessionLocal, ExtractionSession, Contact
from app.contact_processor import ContactProcessor
from app.ews_extractor import EWSExtractor

logger = logging.getLogger(__name__)
ews_router = APIRouter()


async def _ews_task(session_id: str, email: str, password: str, server: str):
    async with SessionLocal() as db:
        s = (await db.execute(
            select(ExtractionSession).where(ExtractionSession.id == session_id)
        )).scalar_one()
        try:
            ext = EWSExtractor(email, password, server)

            def _sync():
                ext.connect()
                return ext.extract()

            # exchangelib est synchrone -> on l'exécute dans un thread
            raw = await asyncio.to_thread(_sync)
            s.total_emails = len(raw)
            await db.commit()

            proc = ContactProcessor(db, session_id)
            for cd in raw:
                try:
                    await proc.process_contact(cd)
                except Exception:
                    await db.rollback()
            await proc.finalize_processing()
            await proc.deduplicate_contacts()

            cnt = (await db.execute(
                select(func.count(Contact.id)).where(Contact.session_id == session_id)
            )).scalar()
            s.status = "completed"
            s.total_contacts = cnt
            s.date_fin = datetime.utcnow()
            await db.commit()
            logger.info(f"EWS terminé pour {email}: {cnt} contacts")
        except Exception as e:
            logger.error(f"Erreur extraction EWS ({email}): {e}", exc_info=True)
            try:
                await db.rollback()
                s.status = "error"
                s.erreur_message = str(e)[:500]
                s.date_fin = datetime.utcnow()
                await db.commit()
            except Exception:
                pass


@ews_router.post("/extract-ews")
async def extract_ews(request: Request, background_tasks: BackgroundTasks,
                      db: AsyncSession = Depends(get_db)):
    """Démarre une extraction sur une boîte Exchange hébergée (EWS)."""
    body = await request.json()
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    server = (body.get("server") or "").strip() or None
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email et mot de passe requis.")

    session_id = str(uuid.uuid4())
    s = ExtractionSession(id=session_id, user_id="ews:" + email,
                          email_address=email, status="in_progress")
    db.add(s)
    await db.commit()

    background_tasks.add_task(_ews_task, session_id, email, password, server)
    return {"session_id": session_id, "status": "in_progress",
            "message": "Extraction Exchange hébergé démarrée"}
