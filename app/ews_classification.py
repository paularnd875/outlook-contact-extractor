"""
Pré-classification IA des contacts issus d'une extraction Exchange hébergé (EWS).

Réutilise exactement le même classifieur LLM (serveur EU) et les mêmes catégories
que la version Microsoft 365. La seule différence : les échanges d'un contact ne
proviennent pas de Microsoft Graph mais des OBJETS de mails collectés pendant
l'extraction EWS (self.excerpts de l'extracteur).

Confidentialité : on ne lit ni ne stocke le corps des mails ; seuls les objets
servent d'indice, et seul le RÉSULTAT (label + justification + confiance) est persisté.
"""

import asyncio
import logging

from sqlalchemy import select

from app.database import SessionLocal, Contact
from app.classifier import classify_contact
from app.ai_profiler import DEFAULT_PROFILES
from app.enriched_classification import _domain_guess

logger = logging.getLogger(__name__)

# Progression par session (partagée avec l'endpoint de statut)
PROGRESS: dict = {}


def _format_excerpts(items, head: int = 2, tail: int = 3) -> str:
    """Formate quelques objets de mails (premiers + derniers) pour le prompt."""
    items = sorted(items, key=lambda x: x[0])
    selected = items[:head] + items[-tail:] if len(items) > head + tail else items
    lines = []
    for dt, direction, subject in selected:
        d = dt.strftime("%Y-%m-%d")
        subj = (subject or "").strip() or "(sans objet)"
        lines.append(f"[{d} {direction}] objet: {subj}")
    return "\n".join(lines)


async def run(session_id: str, extractor, min_emails: int = 2):
    """Classe tous les contacts d'une session EWS. `extractor` porte self.excerpts."""
    profiles = [p.dict() for p in DEFAULT_PROFILES]
    owner_name = getattr(extractor, "owner_name", "") or ""
    owner_email = getattr(extractor, "owner_email", "") or ""
    excerpts_by_email = getattr(extractor, "excerpts", {}) or {}
    sem = asyncio.Semaphore(4)

    async with SessionLocal() as db:
        contacts = (await db.execute(
            select(Contact).where(Contact.session_id == session_id)
        )).scalars().all()

    p = PROGRESS.setdefault(session_id, {})
    p.update({"cls_total": len(contacts), "cls_done": 0, "cls_ai": 0, "cls_errors": 0})

    async def handle(contact: Contact):
        async with sem:
            try:
                items = excerpts_by_email.get(contact.email, [])
                ai_prenom = ai_nom = ""
                real_first = real_last = None

                if (contact.nombre_emails or 0) < min_emails and not items:
                    # échange unique et aucun objet exploitable -> indice domaine ou "autre"
                    dg = _domain_guess(contact.email)
                    if dg:
                        cls, conf = dg, 55
                        just = f"Déduit du domaine « {contact.email.split('@')[-1]} » (un seul échange)."
                    else:
                        cls, conf = "autre", 30
                        just = "Un seul échange — contact ponctuel ou automatique."
                    is_rule = True
                else:
                    dates = sorted(it[0] for it in items) if items else []
                    ctx = {
                        "name": contact.nom_complet or contact.email,
                        "email": contact.email,
                        "count": contact.nombre_emails or len(items),
                        "first": dates[0].strftime("%Y-%m-%d") if dates else (
                            contact.date_premier_contact.strftime("%Y-%m-%d") if contact.date_premier_contact else "?"),
                        "last": dates[-1].strftime("%Y-%m-%d") if dates else (
                            contact.date_dernier_contact.strftime("%Y-%m-%d") if contact.date_dernier_contact else "?"),
                        "excerpts": _format_excerpts(items) if items else "",
                        "owner_name": owner_name,
                        "owner_email": owner_email,
                    }
                    res = await classify_contact(ctx, profiles)
                    cls, conf, just = res["classification"], res["confidence"], res["justification"]
                    ai_prenom, ai_nom = res.get("prenom", ""), res.get("nom", "")
                    if dates:
                        real_first, real_last = dates[0], dates[-1]
                    is_rule = False

                async with SessionLocal() as wdb:
                    c = (await wdb.execute(
                        select(Contact).where(Contact.id == contact.id))).scalar_one()
                    c.classification = cls
                    c.confiance_classification = conf
                    c.justification_classification = just
                    # nom/prénom déduits par l'IA UNIQUEMENT si absents
                    if not (c.prenom or "").strip() and (ai_prenom or ai_nom):
                        if ai_prenom:
                            c.prenom = ai_prenom
                        if ai_nom:
                            c.nom = ai_nom
                        if not (c.nom_complet or "").strip() or "@" in (c.nom_complet or ""):
                            c.nom_complet = f"{ai_prenom} {ai_nom}".strip()
                    if real_last:
                        c.date_dernier_contact = real_last
                    if real_first:
                        c.date_premier_contact = real_first
                    await wdb.commit()

                if not is_rule:
                    p["cls_ai"] += 1
            except Exception as e:
                logger.error(f"Erreur classification EWS {contact.email}: {e}")
                p["cls_errors"] += 1
            finally:
                p["cls_done"] += 1

    await asyncio.gather(*(handle(c) for c in contacts))
    logger.info(f"Classification EWS terminée session {session_id}: {p}")
