"""
Pré-classification IA des contacts issus d'une extraction Exchange hébergé (EWS).

Réutilise exactement le même classifieur LLM (serveur EU) et les mêmes catégories
que la version Microsoft 365. Les échanges d'un contact proviennent des extraits
(objets + corps tronqués) collectés à l'extraction, CHIFFRÉS en base, déchiffrés
uniquement le temps de la classification, puis PURGÉS.

Confidentialité : les extraits ne sont jamais journalisés et sont effacés
(exchanges_enc = NULL) dès qu'un contact est classé (purge=True).
"""

import json
import asyncio
import logging
from datetime import datetime

from sqlalchemy import select

from app.database import SessionLocal, Contact, ExtractionSession
from app.classifier import classify_contact
from app.ai_profiler import DEFAULT_PROFILES
from app.enriched_classification import _domain_guess
from app.crypto_store import encrypt, decrypt

logger = logging.getLogger(__name__)

# Progression par session (partagée avec les endpoints de statut)
PROGRESS: dict = {}


def serialize_excerpts(items) -> str:
    """items: liste de tuples (dt, direction, objet, corps) -> JSON chiffrable."""
    out = []
    for it in items or []:
        dt, direction, subj, body = (list(it) + [None, None, None, None])[:4]
        out.append({
            "d": dt.strftime("%Y-%m-%d") if isinstance(dt, datetime) else "",
            "dir": direction or "",
            "subj": (subj or "")[:200],
            "body": (body or "")[:400],
        })
    return json.dumps(out, ensure_ascii=False)


def encrypt_excerpts(items) -> str:
    """Sérialise puis chiffre les extraits d'un contact. '' si rien."""
    if not items:
        return ""
    return encrypt(serialize_excerpts(items))


def _load_items(enc: str):
    raw = decrypt(enc) if enc else ""
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _format_excerpts(items, head: int = 2, tail: int = 3) -> str:
    """Formate quelques échanges (premiers + derniers) pour le prompt."""
    items = sorted(items, key=lambda x: x.get("d", ""))
    selected = items[:head] + items[-tail:] if len(items) > head + tail else items
    lines = []
    for it in selected:
        d = it.get("d") or "?"
        direction = it.get("dir") or ""
        subj = (it.get("subj") or "").strip() or "(sans objet)"
        body = (it.get("body") or "").strip()
        line = f"[{d} {direction}] objet: {subj}"
        if body:
            line += f" — {body}"
        lines.append(line)
    return "\n".join(lines)


async def run(session_id: str, purge: bool = True, min_emails: int = 2):
    """Classe tous les contacts d'une session à partir des extraits chiffrés en base.
    Si purge=True, efface les extraits (exchanges_enc) dès qu'un contact est classé."""
    profiles = [p.dict() for p in DEFAULT_PROFILES]

    async with SessionLocal() as db:
        session = (await db.execute(
            select(ExtractionSession).where(ExtractionSession.id == session_id)
        )).scalar_one_or_none()
        owner_name = (session.owner_name or "") if session else ""
        owner_email = (session.email_address or "") if session else ""
        # snapshot en données brutes (pas d'ORM détaché après fermeture de session)
        rows = (await db.execute(
            select(Contact.id, Contact.email, Contact.nombre_emails, Contact.nom_complet,
                   Contact.date_premier_contact, Contact.date_dernier_contact, Contact.exchanges_enc)
            .where(Contact.session_id == session_id)
        )).all()
        if session:
            session.ai_status = "running"
            await db.commit()

    sem = asyncio.Semaphore(4)
    p = PROGRESS.setdefault(session_id, {})
    p.update({"cls_total": len(rows), "cls_done": 0, "cls_ai": 0, "cls_errors": 0})

    async def handle(row):
        async with sem:
            try:
                cid, email, nombre_emails, nom_complet, dp, dd, enc = row
                items = _load_items(enc)
                ai_prenom = ai_nom = ""
                real_first = real_last = None
                dates = sorted(d for d in (it.get("d") for it in items) if d)

                if (nombre_emails or 0) < min_emails and not items:
                    dg = _domain_guess(email)
                    if dg:
                        cls, conf = dg, 55
                        just = f"Déduit du domaine « {email.split('@')[-1]} » (un seul échange)."
                    else:
                        cls, conf = "autre", 30
                        just = "Un seul échange — contact ponctuel ou automatique."
                    is_rule = True
                else:
                    ctx = {
                        "name": nom_complet or email,
                        "email": email,
                        "count": nombre_emails or len(items),
                        "first": dates[0] if dates else (dp.strftime("%Y-%m-%d") if dp else "?"),
                        "last": dates[-1] if dates else (dd.strftime("%Y-%m-%d") if dd else "?"),
                        "excerpts": _format_excerpts(items) if items else "",
                        "owner_name": owner_name,
                        "owner_email": owner_email,
                    }
                    res = await classify_contact(ctx, profiles)
                    cls, conf, just = res["classification"], res["confidence"], res["justification"]
                    ai_prenom, ai_nom = res.get("prenom", ""), res.get("nom", "")
                    is_rule = False
                    if dates:
                        try:
                            real_first = datetime.strptime(dates[0], "%Y-%m-%d")
                            real_last = datetime.strptime(dates[-1], "%Y-%m-%d")
                        except Exception:
                            pass

                async with SessionLocal() as wdb:
                    c = (await wdb.execute(
                        select(Contact).where(Contact.id == cid))).scalar_one()
                    c.classification = cls
                    c.confiance_classification = conf
                    c.justification_classification = just
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
                    if purge:
                        c.exchanges_enc = None  # PURGE des données sensibles
                    await wdb.commit()

                if not is_rule:
                    p["cls_ai"] += 1
            except Exception as e:
                logger.error(f"Erreur classification EWS {contact.email}: {e}")
                p["cls_errors"] += 1
            finally:
                p["cls_done"] += 1

    await asyncio.gather(*(handle(r) for r in rows))

    async with SessionLocal() as db:
        s = (await db.execute(
            select(ExtractionSession).where(ExtractionSession.id == session_id)
        )).scalar_one_or_none()
        if s:
            s.ai_status = "done"
            await db.commit()
    p["status"] = "completed"
    logger.info(f"Classification EWS terminée session {session_id}: {p}")


async def purge_excerpts(session_id: str) -> int:
    """Efface les extraits chiffrés d'une session sans classer (associé qui refuse l'IA)."""
    async with SessionLocal() as db:
        contacts = (await db.execute(
            select(Contact).where(Contact.session_id == session_id,
                                  Contact.exchanges_enc.isnot(None))
        )).scalars().all()
        n = 0
        for c in contacts:
            c.exchanges_enc = None
            n += 1
        s = (await db.execute(
            select(ExtractionSession).where(ExtractionSession.id == session_id)
        )).scalar_one_or_none()
        if s:
            s.ai_status = "purged"
        await db.commit()
    logger.info(f"Extraits purgés session {session_id}: {n} contacts")
    return n
