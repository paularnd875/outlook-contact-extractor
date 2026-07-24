"""
Espace ADMIN (opérateur) — réservé à toi, protégé par ADMIN_KEY.

Permet, extraction par extraction (associé par associé), de :
  - lancer la pré-classification IA à la demande (déchiffre les extraits, classe, PURGE),
  - purger les extraits sans classer (associé qui refuse l'IA),
  - télécharger le CSV.

Les associés n'y ont pas accès. Rien ne déclenche le GPU sans ton action.
"""

import os
import logging

from fastapi import APIRouter, Request, HTTPException, BackgroundTasks, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func

from app.database import SessionLocal, Contact, ExtractionSession
from app.ews_classification import run as run_classification, purge_excerpts, PROGRESS

logger = logging.getLogger(__name__)
admin_router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _admin_key() -> str:
    return os.getenv("ADMIN_KEY", "")


def _require_admin(request: Request):
    if not request.session.get("is_admin"):
        raise HTTPException(status_code=401, detail="Accès admin requis.")


@admin_router.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request, key: str = Query(default="")):
    """Page admin. Accès par ?key=ADMIN_KEY (mémorisé en session)."""
    configured = _admin_key()
    if not configured:
        return HTMLResponse("<h3>ADMIN_KEY non configurée sur le serveur.</h3>"
                            "<p>Définis la variable d'environnement ADMIN_KEY puis recharge.</p>",
                            status_code=403)
    if key:
        if key == configured:
            request.session["is_admin"] = True
            return RedirectResponse(url="/admin", status_code=302)
        return HTMLResponse("<h3>Clé invalide.</h3>", status_code=403)
    if not request.session.get("is_admin"):
        # petit formulaire de saisie de la clé
        return HTMLResponse(
            "<div style='font-family:sans-serif;max-width:420px;margin:80px auto;color:#ddd;background:#222;padding:24px;border-radius:8px'>"
            "<h3>Espace admin</h3><form method='get' action='/admin'>"
            "<input name='key' type='password' placeholder='Clé admin' style='width:100%;padding:8px;margin:8px 0'>"
            "<button type='submit' style='padding:8px 16px'>Entrer</button></form></div>")

    # Liste des sessions EWS avec stats
    async with SessionLocal() as db:
        sessions = (await db.execute(
            select(ExtractionSession)
            .where(ExtractionSession.user_id.like("ews:%"))
            .order_by(ExtractionSession.date_debut.desc())
        )).scalars().all()
        rows = []
        for s in sessions:
            total = (await db.execute(select(func.count(Contact.id)).where(
                Contact.session_id == s.id))).scalar() or 0
            classified = (await db.execute(select(func.count(Contact.id)).where(
                Contact.session_id == s.id, Contact.classification.isnot(None)))).scalar() or 0
            with_exc = (await db.execute(select(func.count(Contact.id)).where(
                Contact.session_id == s.id, Contact.exchanges_enc.isnot(None)))).scalar() or 0
            rows.append({
                "id": s.id, "email": s.email_address,
                "date": s.date_debut.strftime("%Y-%m-%d %H:%M") if s.date_debut else "",
                "total": total, "classified": classified, "with_exc": with_exc,
                "ai_status": s.ai_status or "",
            })

    from app.classifier import ping
    llm = await ping()
    return templates.TemplateResponse("admin.html", {
        "request": request, "rows": rows, "llm": llm,
    })


@admin_router.post("/admin/classify")
async def admin_classify(request: Request, background_tasks: BackgroundTasks,
                         session_id: str = Query(...)):
    """Lance la pré-classification IA (déchiffre extraits -> classe -> purge)."""
    _require_admin(request)
    from app.classifier import ping
    llm = await ping()
    if not llm.get("ok"):
        raise HTTPException(status_code=503,
            detail="Serveur d'IA injoignable. Allume-le (Scaleway) puis réessaie.")
    PROGRESS[session_id] = {"cls_total": 0, "cls_done": 0, "cls_ai": 0, "cls_errors": 0, "status": "running"}
    background_tasks.add_task(run_classification, session_id, True)
    return {"message": "Classification lancée", "session_id": session_id}


@admin_router.post("/admin/purge")
async def admin_purge(request: Request, session_id: str = Query(...)):
    """Purge les extraits chiffrés d'une session sans classer."""
    _require_admin(request)
    n = await purge_excerpts(session_id)
    return {"message": f"{n} extraits purgés", "session_id": session_id}


@admin_router.get("/admin/status/{session_id}")
async def admin_status(request: Request, session_id: str):
    """Progression de la classification admin."""
    _require_admin(request)
    return PROGRESS.get(session_id, {"status": "idle", "cls_done": 0, "cls_total": 0})
