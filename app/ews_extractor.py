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

    def _address_book(self) -> List[Dict]:
        out = []
        try:
            for c in self.account.contacts.all():
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
        except Exception as e:
            logger.error(f"EWS lecture carnet: {e}")
        return out

    def _from_emails(self, max_per_folder: int = 6000) -> List[Dict]:
        out = []
        folders = []
        for attr in ("inbox", "sent"):
            f = getattr(self.account, attr, None)
            if f is not None:
                folders.append(f)
        for folder in folders:
            try:
                qs = folder.all().only("sender", "to_recipients", "cc_recipients",
                                       "datetime_received", "datetime_sent", "message_id")
                n = 0
                for msg in qs:
                    n += 1
                    if n > max_per_folder:
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
            except Exception as e:
                logger.error(f"EWS lecture dossier {getattr(folder,'name','?')}: {e}")
        return out

    def extract(self) -> List[Dict]:
        """Retourne la liste des contacts (carnet + emails). Bloquant (sync)."""
        contacts = self._address_book()
        logger.info(f"EWS: {len(contacts)} entrées de carnet")
        emails_contacts = self._from_emails()
        logger.info(f"EWS: {len(emails_contacts)} occurrences depuis les emails")
        contacts.extend(emails_contacts)
        return contacts
