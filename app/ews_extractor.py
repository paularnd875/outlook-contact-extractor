"""
Extracteur de contacts pour les boîtes Exchange HÉBERGÉES (hors Microsoft 365),
via EWS (Exchange Web Services) et la librairie exchangelib.

Cible : Hosted Exchange type Infoclip / cloudexchange.fr / SolidCP, etc.
Lit le carnet de contacts ET les emails (Réception + Envoyés) pour en déduire
les contacts, au même format que GraphExtractor (pour réutiliser ContactProcessor).
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _naive(dt) -> datetime:
    if dt is None:
        return datetime.utcnow()
    try:
        return dt.replace(tzinfo=None)
    except Exception:
        try:
            return datetime.fromisoformat(str(dt)).replace(tzinfo=None)
        except Exception:
            return datetime.utcnow()


def _split_name(nom_complet: Optional[str]):
    """Nettoie et découpe un nom complet en (nom, prénom)."""
    from app.normalizer import clean_person_name
    cleaned = clean_person_name(nom_complet)
    if not cleaned:
        return None, None
    parts = cleaned.split()
    if len(parts) == 1:
        return parts[0], None
    return " ".join(parts[1:]), parts[0]  # (nom, prenom)


class EWSExtractor:
    """Extracteur via EWS pour Exchange hébergé."""

    def __init__(self, email: str, password: str, server: Optional[str] = None):
        self.email = email
        self.password = password
        self.server = (server or "").strip() or None
        self.owner_email = (email or "").lower().strip()
        self.account = None

    def connect(self):
        from exchangelib import Credentials, Account, Configuration, DELEGATE
        creds = Credentials(username=self.email, password=self.password)
        if self.server:
            config = Configuration(server=self.server, credentials=creds)
            self.account = Account(self.email, config=config, autodiscover=False, access_type=DELEGATE)
        else:
            self.account = Account(self.email, credentials=creds, autodiscover=True, access_type=DELEGATE)
        _ = self.account.root.total_count  # force un appel réel -> valide la connexion
        logger.info(f"EWS connecté: {self.email} ({self.account.version})")
        return self.account

    def _iter_all_folders(self):
        """Parcourt récursivement l'arbre de dossiers VISIBLE de l'utilisateur
        (mails + carnets, sous-dossiers et archives compris), en évitant les
        dizaines de dossiers système cachés. Déduplique par identifiant."""
        seen = set()
        candidates = []
        # dossiers bien connus d'abord (au cas où walk() en manquerait un)
        for attr in ("inbox", "sent", "contacts", "drafts", "outbox"):
            f = getattr(self.account, attr, None)
            if f is not None:
                candidates.append(f)
        # arbre utilisateur : "Haut de la banque d'informations"
        root = getattr(self.account, "msg_folder_root", None) or getattr(self.account, "root", None)
        if root is not None:
            try:
                for f in root.walk():
                    candidates.append(f)
            except Exception as e:
                logger.error(f"EWS walk: {e}")
        for f in candidates:
            fid = getattr(f, "id", None) or id(f)
            if fid in seen:
                continue
            seen.add(fid)
            yield f

    def _read_contacts(self, folder) -> List[Dict]:
        out = []
        for c in folder.all():
            email = None
            for ea in (getattr(c, "email_addresses", None) or []):
                if getattr(ea, "email", None):
                    email = ea.email.lower().strip()
                    break
            if not email or "@" not in email:
                continue
            out.append({
                "email": email,
                "prenom": getattr(c, "given_name", None),
                "nom": getattr(c, "surname", None),
                "nom_complet": getattr(c, "display_name", None),
                "intitule": getattr(c, "job_title", None),
                "type_contact": "carnet",
                "date_contact": _naive(getattr(c, "last_modified_time", None)),
                "source_email_id": None,
            })
        return out

    def _read_mail(self, folder, remaining: int) -> List[Dict]:
        out = []
        qs = folder.all().only("sender", "to_recipients", "cc_recipients",
                               "datetime_received", "datetime_sent", "message_id")
        n = 0
        for msg in qs:
            n += 1
            if n > remaining:
                logger.warning(f"EWS: plafond atteint dans {getattr(folder,'name','?')}")
                break
            dt = _naive(getattr(msg, "datetime_received", None) or getattr(msg, "datetime_sent", None))
            mid = getattr(msg, "message_id", None)
            people = []
            s = getattr(msg, "sender", None)
            if s and getattr(s, "email_address", None):
                people.append((s.name, s.email_address, "sender"))
            for attr in ("to_recipients", "cc_recipients"):
                for r in (getattr(msg, attr, None) or []):
                    if getattr(r, "email_address", None):
                        people.append((r.name, r.email_address, "recipient"))
            for name, addr, typ in people:
                addr = (addr or "").lower().strip()
                if not addr or "@" not in addr or addr == self.owner_email:
                    continue
                nom, prenom = _split_name(name)
                out.append({
                    "email": addr, "nom_complet": name, "nom": nom, "prenom": prenom,
                    "type_contact": typ, "date_contact": dt, "source_email_id": mid,
                })
        return out

    def extract(self, global_cap: int = 300000) -> List[Dict]:
        """Retourne la liste des contacts depuis TOUS les dossiers (carnets + mails,
        récursif, sous-dossiers/archives compris). Bloquant (sync).
        Ignore Spam et Corbeille pour éviter les contacts parasites."""
        # ids des dossiers à exclure (spam / corbeille)
        skip_ids = set()
        for attr in ("junk", "trash"):
            f = getattr(self.account, attr, None)
            fid = getattr(f, "id", None) if f is not None else None
            if fid:
                skip_ids.add(fid)

        contacts = []
        mail_folders = contact_folders = mail_seen = 0
        for folder in self._iter_all_folders():
            if getattr(folder, "id", None) in skip_ids:
                continue
            cc = getattr(folder, "CONTAINER_CLASS", None) or ""
            try:
                if cc.startswith("IPF.Contact"):
                    # carnet ou sous-carnet (Sociétés, GAL, cache de destinataires...)
                    entries = self._read_contacts(folder)
                    contacts.extend(entries)
                    contact_folders += 1
                elif cc == "IPF.Note":
                    # dossier mail (Réception, Envoyés, et TOUS les sous-dossiers/archives)
                    remaining = global_cap - mail_seen
                    if remaining <= 0:
                        logger.warning("EWS: plafond global atteint, arrêt lecture mails")
                        break
                    entries = self._read_mail(folder, remaining)
                    contacts.extend(entries)
                    mail_seen += len(entries)
                    mail_folders += 1
                # autres classes (calendrier, tâches, notes, dossiers système) : ignorées
            except Exception as e:
                logger.error(f"EWS lecture dossier {getattr(folder,'name','?')} ({cc}): {e}")
        logger.info(f"EWS: {contact_folders} carnets + {mail_folders} dossiers mail parcourus "
                    f"-> {len(contacts)} occurrences de contacts")
        return contacts
