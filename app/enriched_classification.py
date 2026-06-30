"""
Classification enrichie : pour chaque contact "à signal", on récupère TOUS ses
échanges email (sur la période), on les lit en mémoire, on les envoie au LLM local,
puis on persiste UNIQUEMENT le résultat (label + justification + confiance).

Confidentialité : les corps d'emails ne sont jamais écrits en base ni dans les logs,
et sont libérés dès que le contact est classé.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks, Query
from sqlalchemy import select

from app.database import SessionLocal, Contact
from app.classifier import classify_contact, LLM_MODEL, LLM_URL

logger = logging.getLogger(__name__)
enriched_router = APIRouter()

GRAPH = "https://graph.microsoft.com/v1.0"

# Mode produit : classification IA activée ou non (extraction seule).
# Permet de servir des clients "extraction seule" et d'autres "extraction + IA".
CLASSIFICATION_ENABLED = os.getenv("CLASSIFICATION_ENABLED", "true").lower() in ("1", "true", "yes", "oui")

# Suivi de progression en mémoire, par session
PROGRESS: dict = {}

# Marqueurs de citation pour couper l'historique repris dans chaque message
_QUOTE_MARKERS = [
    r"^\s*-+\s*Message d'origine",
    r"^\s*-+\s*Original Message",
    r"^\s*De\s*:",
    r"^\s*From\s*:",
    r"^\s*Le .*a écrit\s*:",
    r"^\s*On .*wrote\s*:",
    r"^\s*>",
    r"^\s*_{5,}",
]
_QUOTE_RE = re.compile("|".join(_QUOTE_MARKERS), re.IGNORECASE | re.MULTILINE)


def _clean_message_text(text: str, max_len: int = 200) -> str:
    """Garde la partie utile d'un message (coupe l'historique cité), tronque."""
    if not text:
        return ""
    m = _QUOTE_RE.search(text)
    if m:
        text = text[:m.start()]
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return text[:max_len]


async def _fetch_thread(client: httpx.AsyncClient, headers: dict, email: str, start_date: datetime) -> list:
    """Récupère les messages échangés avec un contact (recherche par participant)."""
    params = {
        "$search": f'"participants:{email}"',
        "$select": "subject,from,toRecipients,sentDateTime,receivedDateTime,body",
        "$top": 50,
    }
    # Prefer: corps en texte brut (pas de HTML à parser)
    h = dict(headers)
    h["Prefer"] = 'outlook.body-content-type="text"'
    try:
        r = await client.get(f"{GRAPH}/me/messages", headers=h, params=params)
        r.raise_for_status()
        messages = r.json().get("value", [])
    except Exception as e:
        logger.warning(f"Fetch thread échoué pour {email}: {e}")
        return []

    items = []
    for msg in messages:
        date_str = msg.get("receivedDateTime") or msg.get("sentDateTime")
        if not date_str:
            continue
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            continue
        if dt < start_date:
            continue
        sender = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
        direction = "R" if sender == email.lower() else "E"
        body = (msg.get("body") or {}).get("content", "")
        items.append({
            "dt": dt,
            "subject": (msg.get("subject") or "").strip(),
            "direction": direction,
            "text": _clean_message_text(body),
        })
    items.sort(key=lambda x: x["dt"])
    return items


def _build_excerpts(items: list, head: int = 2, tail: int = 3) -> str:
    """Sélectionne les premiers et derniers messages et formate les extraits."""
    if len(items) > head + tail:
        selected = items[:head] + items[-tail:]
    else:
        selected = items
    lines = []
    for it in selected:
        date = it["dt"].strftime("%Y-%m-%d")
        subj = f" (objet: {it['subject']})" if it["subject"] else ""
        body = it["text"] or it["subject"] or "(vide)"
        lines.append(f"[{date} {it['direction']}]{subj} {body}")
    return "\n".join(lines)


async def _run(session_id: str, user_id: str, min_emails: int, period_months: float, limit: int = 0):
    """Tâche de fond : classe les contacts de la session."""
    from app.auth import get_user_token
    from app.ai_profiler import DEFAULT_PROFILES

    profiles = [p.dict() for p in DEFAULT_PROFILES]
    start_date = datetime.utcnow() - timedelta(days=period_months * 30)
    sem = asyncio.Semaphore(4)

    async with SessionLocal() as db:
        result = await db.execute(select(Contact).where(Contact.session_id == session_id))
        contacts = result.scalars().all()

    # Mode test : ne traiter que les N contacts les plus actifs (vrais fils)
    if limit and limit > 0:
        contacts = sorted(contacts, key=lambda c: c.nombre_emails or 0, reverse=True)[:limit]

    PROGRESS[session_id] = {"total": len(contacts), "done": 0, "status": "running",
                            "rules": 0, "ai": 0, "errors": 0}
    p = PROGRESS[session_id]

    token = get_user_token(user_id)
    if not token:
        p["status"] = "error"
        p["message"] = "Token d'accès introuvable ou expiré — reconnectez-vous."
        return
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=60.0) as client:
        async def handle(contact: Contact):
            async with sem:
                # Token rafraîchi à la volée (sessions longues)
                t = get_user_token(user_id)
                hh = {"Authorization": f"Bearer {t}"} if t else headers
                try:
                    if (contact.nombre_emails or 0) < min_emails:
                        cls, conf = "autre", 30
                        just = f"Un seul échange sur la période ({contact.nombre_emails or 0}) — contact ponctuel ou automatique."
                        is_rule = True
                    else:
                        items = await _fetch_thread(client, hh, contact.email, start_date)
                        if not items:
                            cls, conf = "autre", 20
                            just = "Aucun échange récupérable sur la période."
                            is_rule = True
                        else:
                            ctx = {
                                "name": contact.nom_complet or contact.email,
                                "email": contact.email,
                                "count": contact.nombre_emails or len(items),
                                "first": items[0]["dt"].strftime("%Y-%m-%d"),
                                "last": items[-1]["dt"].strftime("%Y-%m-%d"),
                                "excerpts": _build_excerpts(items),
                            }
                            res = await classify_contact(ctx, profiles)
                            cls, conf, just = res["classification"], res["confidence"], res["justification"]
                            is_rule = False
                            # Libération explicite du contenu brut
                            del items, ctx

                    # Persistance du SEUL résultat
                    async with SessionLocal() as wdb:
                        r = await wdb.execute(select(Contact).where(Contact.id == contact.id))
                        c = r.scalar_one()
                        c.classification = cls
                        c.confiance_classification = conf
                        c.justification_classification = just
                        await wdb.commit()

                    p["rules" if is_rule else "ai"] += 1
                except Exception as e:
                    logger.error(f"Erreur classification enrichie {contact.email}: {e}")
                    p["errors"] += 1
                finally:
                    p["done"] += 1

        await asyncio.gather(*(handle(c) for c in contacts))

    p["status"] = "completed"
    logger.info(f"Classification enrichie terminée session {session_id}: {p}")


@enriched_router.post("/classify-enriched")
async def classify_enriched(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: str = Query(...),
    min_emails: int = Query(default=2, ge=1),
    period_months: float = Query(default=12, ge=0.25, le=120),
    limit: int = Query(default=0, ge=0),
):
    """Démarre la classification enrichie (lecture des fils) en arrière-plan."""
    if not CLASSIFICATION_ENABLED:
        raise HTTPException(status_code=403,
            detail="Classification IA désactivée pour cette instance (mode extraction seule).")

    from app.auth import user_tokens

    user_id = request.session.get("user_id")
    if not user_id or user_id not in user_tokens:
        if len(user_tokens) == 1:
            user_id = next(iter(user_tokens))
        else:
            raise HTTPException(status_code=401,
                detail="Aucun compte connecté. Reconnectez-vous avec la boîte à analyser.")

    background_tasks.add_task(_run, session_id, user_id, min_emails, period_months, limit)
    return {"message": "Classification enrichie démarrée", "session_id": session_id, "model": LLM_MODEL, "llm_url": LLM_URL}


@enriched_router.get("/classify-enriched/{session_id}/status")
async def classify_enriched_status(session_id: str):
    """Progression de la classification enrichie."""
    return PROGRESS.get(session_id, {"status": "idle", "done": 0, "total": 0})
