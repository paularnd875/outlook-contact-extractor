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
        # NB: pas de filtre de date ici -> on utilise TOUT l'historique du contact
        # (la recherche renvoie ses messages ; le LLM se sert des dates pour la récence).
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


def _domain_guess(email: str):
    """Devine un type de contact d'après le domaine de l'email (indice rapide, sans IA)."""
    dom = email.split("@")[-1].lower() if "@" in email else ""
    if any(k in dom for k in ("avocat", "barreau", "-law.", ".law", "law-")):
        return "avocat"
    if "notaire" in dom:
        return "partenaire"
    return None


async def _run(session_id: str, user_id: str, min_emails: int, period_months: float, limit: int = 0, redo_no_thread: bool = False, only_missing_name: bool = False):
    """Tâche de fond : classe les contacts de la session."""
    from app.auth import get_user_token
    from app.ai_profiler import DEFAULT_PROFILES

    profiles = [p.dict() for p in DEFAULT_PROFILES]
    start_date = datetime.utcnow() - timedelta(days=period_months * 30)
    sem = asyncio.Semaphore(4)

    # Token + propriétaire d'abord (le propriétaire sert au filtre correctif ET au prompt)
    token = get_user_token(user_id)
    if not token:
        PROGRESS[session_id] = {"total": 0, "done": 0, "status": "error", "rules": 0, "ai": 0,
                                "errors": 0, "message": "Token d'accès introuvable ou expiré — reconnectez-vous."}
        return
    headers = {"Authorization": f"Bearer {token}"}

    owner_name, owner_email = "", ""
    try:
        async with httpx.AsyncClient(timeout=30.0) as oc:
            me = await oc.get(f"{GRAPH}/me", headers=headers)
            if me.status_code == 200:
                j = me.json()
                owner_name = j.get("displayName", "")
                owner_email = (j.get("mail") or j.get("userPrincipalName") or "").lower()
    except Exception:
        pass

    async with SessionLocal() as db:
        result = await db.execute(select(Contact).where(Contact.session_id == session_id))
        contacts = result.scalars().all()

    # Mode correctif : re-traiter les contacts dont le fil avait échoué ("Aucun échange")
    # ET ceux dont la justification confondait le contact avec le propriétaire de la boîte.
    if redo_no_thread:
        owner_tokens = [w.lower() for w in (owner_name or "").split() if len(w) > 2]
        def _needs_redo(c):
            j = (c.justification_classification or "")
            if j.startswith("Aucun échange"):
                return True
            jl = j.lower()
            return any(w in jl for w in owner_tokens)
        contacts = [c for c in contacts if _needs_redo(c)]

    # Mode « déduire le nom » : uniquement les contacts sans prénom récupéré d'Outlook
    if only_missing_name:
        contacts = [c for c in contacts if not (c.prenom or "").strip()]

    # Mode test : ne traiter que les N contacts les plus actifs
    if limit and limit > 0:
        contacts = sorted(contacts, key=lambda c: c.nombre_emails or 0, reverse=True)[:limit]

    PROGRESS[session_id] = {"total": len(contacts), "done": 0, "status": "running",
                            "rules": 0, "ai": 0, "errors": 0}
    p = PROGRESS[session_id]

    async with httpx.AsyncClient(timeout=60.0) as client:
        async def handle(contact: Contact):
            async with sem:
                # Token rafraîchi à la volée (sessions longues)
                t = get_user_token(user_id)
                hh = {"Authorization": f"Bearer {t}"} if t else headers
                try:
                    ai_prenom, ai_nom = "", ""
                    real_last = real_first = None
                    if (contact.nombre_emails or 0) < min_emails:
                        # 1 seul échange : indice domaine si évident, sinon "autre"
                        dg = _domain_guess(contact.email)
                        if dg:
                            cls, conf = dg, 55
                            just = f"Déduit du domaine professionnel « {contact.email.split('@')[-1]} » (un seul échange)."
                        else:
                            cls, conf = "autre", 30
                            just = f"Un seul échange ({contact.nombre_emails or 0}) — contact ponctuel ou automatique."
                        is_rule = True
                    else:
                        items = await _fetch_thread(client, hh, contact.email, start_date)
                        # Même sans fil récupérable, on laisse l'IA classer d'après nom/email/domaine
                        ctx = {
                            "name": contact.nom_complet or contact.email,
                            "email": contact.email,
                            "count": contact.nombre_emails or len(items),
                            "first": items[0]["dt"].strftime("%Y-%m-%d") if items else "?",
                            "last": items[-1]["dt"].strftime("%Y-%m-%d") if items else "?",
                            "excerpts": _build_excerpts(items) if items else "",
                            "owner_name": owner_name,
                            "owner_email": owner_email,
                        }
                        res = await classify_contact(ctx, profiles)
                        cls, conf, just = res["classification"], res["confidence"], res["justification"]
                        ai_prenom, ai_nom = res.get("prenom", ""), res.get("nom", "")
                        is_rule = False
                        # Vraies dates d'échange (corrige la date d'extraction erronée)
                        if items:
                            real_first, real_last = items[0]["dt"], items[-1]["dt"]
                        del items, ctx

                    # Persistance du SEUL résultat
                    async with SessionLocal() as wdb:
                        r = await wdb.execute(select(Contact).where(Contact.id == contact.id))
                        c = r.scalar_one()
                        c.classification = cls
                        c.confiance_classification = conf
                        c.justification_classification = just
                        # Nom/prénom déduits par l'IA UNIQUEMENT si Outlook ne les avait pas
                        if not (c.prenom or "").strip() and (ai_prenom or ai_nom):
                            if ai_prenom:
                                c.prenom = ai_prenom
                            if ai_nom:
                                c.nom = ai_nom
                            if not (c.nom_complet or "").strip() or "@" in (c.nom_complet or ""):
                                c.nom_complet = f"{ai_prenom} {ai_nom}".strip()
                        # Vraies dates d'échange (issues du fil réel)
                        if real_last:
                            c.date_dernier_contact = real_last
                        if real_first:
                            c.date_premier_contact = real_first
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
    redo_no_thread: bool = Query(default=False),
    only_missing_name: bool = Query(default=False),
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

    background_tasks.add_task(_run, session_id, user_id, min_emails, period_months, limit, redo_no_thread, only_missing_name)
    return {"message": "Classification enrichie démarrée", "session_id": session_id, "model": LLM_MODEL, "llm_url": LLM_URL}


@enriched_router.get("/classify-enriched/{session_id}/status")
async def classify_enriched_status(session_id: str):
    """Progression de la classification enrichie."""
    return PROGRESS.get(session_id, {"status": "idle", "done": 0, "total": 0})
