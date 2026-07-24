"""
API pour l'extraction via Exchange hébergé (EWS) — hors Microsoft 365.
Extraction des contacts, puis (option) pré-classification IA avec le même
serveur LLM EU que la version Microsoft 365.
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
from app.enriched_classification import CLASSIFICATION_ENABLED

logger = logging.getLogger(__name__)
ews_router = APIRouter()

# Progression combinée (extraction + classification) par session
PROGRESS: dict = {}


async def _ews_task(session_id: str, email: str, password: str, server: str, classify: bool):
    from app.ews_classification import run as run_classification, encrypt_excerpts, PROGRESS as CLS_PROGRESS

    p = PROGRESS.setdefault(session_id, {})
    p.update({"phase": "extraction", "classify": classify, "status": "in_progress",
              "total_emails": 0, "total_contacts": 0, "message": "Connexion et extraction en cours…"})

    async with SessionLocal() as db:
        s = (await db.execute(
            select(ExtractionSession).where(ExtractionSession.id == session_id)
        )).scalar_one()
        ext = None
        try:
            ext = EWSExtractor(email, password, server)

            def _sync():
                ext.connect()
                return ext.extract()

            raw = await asyncio.to_thread(_sync)
            s.total_emails = len(raw)
            p["total_emails"] = len(raw)
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
            s.total_contacts = cnt
            p["total_contacts"] = cnt

            # Conserver (CHIFFRÉ) les extraits d'échanges par contact, pour une
            # pré-classification IA à la demande plus tard. Purgés après classification.
            s.owner_name = ext.owner_name or None
            stored = 0
            contacts = (await db.execute(
                select(Contact).where(Contact.session_id == session_id))).scalars().all()
            for c in contacts:
                enc = encrypt_excerpts(ext.excerpts.get(c.email, []))
                if enc:
                    c.exchanges_enc = enc
                    stored += 1
            await db.commit()
            logger.info(f"EWS extraction terminée {email}: {cnt} contacts, {stored} avec extraits")

            # --- Pré-classification IA (optionnelle, immédiate) ---
            if classify and CLASSIFICATION_ENABLED and cnt:
                from app.classifier import ping
                llm = await ping()
                if not llm.get("ok"):
                    p["message"] = ("Contacts extraits. Analyse IA ignorée : le serveur d'IA "
                                    "ne répond pas (allumez-le puis relancez).")
                    p["ai_skipped"] = True
                else:
                    p["phase"] = "classification"
                    p["message"] = "Analyse IA des contacts en cours…"
                    # synchronise la progression de classification dans notre PROGRESS
                    CLS_PROGRESS[session_id] = p
                    await run_classification(session_id, purge=True)

            s.status = "completed"
            s.date_fin = datetime.utcnow()
            await db.commit()
            p["phase"] = "done"
            p["status"] = "completed"
            if not p.get("message", "").startswith("Contacts extraits. Analyse IA ignorée"):
                p["message"] = "Terminé."
            logger.info(f"EWS terminé {email}")
        except Exception as e:
            logger.error(f"Erreur EWS ({email}): {e}", exc_info=True)
            try:
                await db.rollback()
                s.status = "error"
                s.erreur_message = str(e)[:500]
                s.date_fin = datetime.utcnow()
                await db.commit()
            except Exception:
                pass
            p["phase"] = "error"
            p["status"] = "error"
            p["message"] = "Échec : impossible de se connecter. Vérifie l'email, le mot de passe et le serveur."


@ews_router.post("/extract-ews")
async def extract_ews(request: Request, background_tasks: BackgroundTasks,
                      db: AsyncSession = Depends(get_db)):
    """Démarre une extraction sur une boîte Exchange hébergée (EWS), avec option IA."""
    body = await request.json()
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    server = (body.get("server") or "").strip() or None
    classify = bool(body.get("classify"))
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email et mot de passe requis.")

    session_id = str(uuid.uuid4())
    s = ExtractionSession(id=session_id, user_id="ews:" + email,
                          email_address=email, status="in_progress")
    db.add(s)
    await db.commit()

    background_tasks.add_task(_ews_task, session_id, email, password, server, classify)
    return {"session_id": session_id, "status": "in_progress",
            "classify": classify and CLASSIFICATION_ENABLED,
            "message": "Extraction Exchange hébergé démarrée"}


@ews_router.get("/ews-progress/{session_id}")
async def ews_progress(session_id: str):
    """Progression combinée extraction + classification IA."""
    return PROGRESS.get(session_id, {"phase": "unknown", "status": "in_progress",
                                      "message": "En attente…"})
