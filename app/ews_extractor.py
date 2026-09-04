"""
Extracteur de contacts pour les boîtes Exchange HÉBERGÉES (hors Microsoft 365),
via EWS (Exchange Web Services) et la librairie exchangelib.

Cible : Hosted Exchange type Infoclip / cloudexchange.fr / SolidCP, etc.
Lit le carnet de contacts ET les emails (Réception + Envoyés) pour en déduire
les contacts, au même format que GraphExtractor (pour réutiliser ContactProcessor).
"""

import os
import re
import time
import logging
from datetime import datetime
from typing import Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)


def _default_global_cap() -> int:
    """Plafond du nombre de MAILS lus par extraction (garde-fou temps de traitement,
    plus la mémoire depuis le passage en streaming). Configurable via EWS_GLOBAL_CAP."""
    try:
        return max(1, int(os.getenv("EWS_GLOBAL_CAP", "300000")))
    except (TypeError, ValueError):
        return 300000


_DEFAULT_GLOBAL_CAP = _default_global_cap()


def _throttle_params():
    """Throttling : une courte pause tous les N mails, pour ESPACER les requêtes EWS.
    Sans ça, une grosse extraction (ex. 253k mails de Marine) envoie des centaines de
    milliers de requêtes en rafale et fait bannir l'IP par le pare-feu de l'hébergeur
    (cloudexchange) -> timeouts de connexion pour TOUT le monde. Réglable sans
    redéploiement via EWS_THROTTLE_EVERY (mails) et EWS_THROTTLE_MS (millisecondes)."""
    try:
        every = max(1, int(os.getenv("EWS_THROTTLE_EVERY", "100")))
    except (TypeError, ValueError):
        every = 100
    try:
        ms = max(0, int(os.getenv("EWS_THROTTLE_MS", "300")))
    except (TypeError, ValueError):
        ms = 300
    return every, ms / 1000.0


_THROTTLE_EVERY, _THROTTLE_SEC = _throttle_params()

# Marqueurs de citation pour couper l'historique repris dans chaque message.
_QUOTE_RE = re.compile(
    r"^\s*-+\s*Message d'origine|^\s*-+\s*Original Message|^\s*De\s*:|^\s*From\s*:"
    r"|^\s*Le .*a écrit\s*:|^\s*On .*wrote\s*:|^\s*>|^\s*_{5,}",
    re.IGNORECASE | re.MULTILINE,
)


def _clean_body(text: str, max_len: int = 400) -> str:
    """Garde la partie utile d'un corps de mail (coupe l'historique cité), tronque."""
    if not text:
        return ""
    m = _QUOTE_RE.search(text)
    if m:
        text = text[:m.start()]
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return text[:max_len]


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
        self.owner_name = ""
        self.account = None
        # Extraits (objets de mails) par contact, pour la pré-classification IA.
        # Léger : on ne stocke que l'objet + date + sens, jamais le corps.
        self.excerpts: Dict[str, list] = {}
        self._max_excerpts = 8

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

    def _read_contacts(self, folder) -> Iterator[Dict]:
        """Génère (yield) les entrées du carnet, une à une (pas d'accumulation)."""
        i = 0
        for c in folder.all():
            i += 1
            if _THROTTLE_SEC and i % _THROTTLE_EVERY == 0:
                time.sleep(_THROTTLE_SEC)
            email = None
            for ea in (getattr(c, "email_addresses", None) or []):
                if getattr(ea, "email", None):
                    email = ea.email.lower().strip()
                    break
            if not email or "@" not in email:
                continue
            yield {
                "email": email,
                "prenom": getattr(c, "given_name", None),
                "nom": getattr(c, "surname", None),
                "nom_complet": getattr(c, "display_name", None),
                "intitule": getattr(c, "job_title", None),
                "type_contact": "carnet",
                "date_contact": _naive(getattr(c, "last_modified_time", None)),
                "source_email_id": None,
            }

    def _read_mail(self, folder, remaining: int) -> Iterator[Dict]:
        """Génère (yield) les occurrences de contacts d'un dossier mail, une à une.
        `remaining` borne le nombre de MAILS lus (pas d'occurrences) pour respecter
        le plafond global. Ne matérialise jamais la liste complète en mémoire."""
        qs = folder.all().only("sender", "to_recipients", "cc_recipients",
                               "datetime_received", "datetime_sent", "message_id",
                               "subject", "text_body")
        n = 0
        for msg in qs:
            n += 1
            if n > remaining:
                logger.warning(f"EWS: plafond atteint dans {getattr(folder,'name','?')}")
                break
            # Throttling : petite pause tous les N mails pour espacer les requêtes EWS
            # (évite de faire bannir l'IP par le pare-feu de l'hébergeur). S'exécute
            # dans le thread producteur -> ne bloque pas la boucle async.
            if _THROTTLE_SEC and n % _THROTTLE_EVERY == 0:
                time.sleep(_THROTTLE_SEC)
            dt = _naive(getattr(msg, "datetime_received", None) or getattr(msg, "datetime_sent", None))
            mid = getattr(msg, "message_id", None)
            subject = (getattr(msg, "subject", None) or "").strip()
            body = _clean_body(getattr(msg, "text_body", None) or "")
            people = []
            s = getattr(msg, "sender", None)
            if s and getattr(s, "email_address", None):
                people.append((s.name, s.email_address, "sender"))
                # mémorise le nom du propriétaire (expéditeur de ses propres envois)
                if not self.owner_name and (s.email_address or "").lower().strip() == self.owner_email and getattr(s, "name", None):
                    self.owner_name = s.name
            for attr in ("to_recipients", "cc_recipients"):
                for r in (getattr(msg, attr, None) or []):
                    if getattr(r, "email_address", None):
                        people.append((r.name, r.email_address, "recipient"))
            for name, addr, typ in people:
                addr = (addr or "").lower().strip()
                if not addr or "@" not in addr or addr == self.owner_email:
                    continue
                nom, prenom = _split_name(name)
                # extrait pour l'IA : R = reçu du contact (il est expéditeur), E = envoyé au contact
                bucket = self.excerpts.setdefault(addr, [])
                if len(bucket) < self._max_excerpts:
                    bucket.append((dt, "R" if typ == "sender" else "E", subject, body))
                yield {
                    "email": addr, "nom_complet": name, "nom": nom, "prenom": prenom,
                    "type_contact": typ, "date_contact": dt, "source_email_id": mid,
                }

    def extract_iter(self, global_cap: int = _DEFAULT_GLOBAL_CAP) -> Iterator[Dict]:
        """Génère (yield) les occurrences de contacts depuis TOUS les dossiers
        (carnets + mails, récursif, sous-dossiers/archives compris), une à une.
        Bloquant (sync) mais SANS accumulation en RAM : le consommateur traite au
        fil de l'eau -> pic mémoire plat, quelle que soit la taille de la boîte.
        Ignore Spam et Corbeille pour éviter les contacts parasites."""
        # ids des dossiers à exclure (spam / corbeille)
        skip_ids = set()
        for attr in ("junk", "trash"):
            f = getattr(self.account, attr, None)
            fid = getattr(f, "id", None) if f is not None else None
            if fid:
                skip_ids.add(fid)

        mail_folders = contact_folders = mail_seen = yielded = 0
        for folder in self._iter_all_folders():
            if getattr(folder, "id", None) in skip_ids:
                continue
            cc = getattr(folder, "CONTAINER_CLASS", None) or ""
            try:
                if cc.startswith("IPF.Contact"):
                    # carnet ou sous-carnet (Sociétés, GAL, cache de destinataires...)
                    for entry in self._read_contacts(folder):
                        yielded += 1
                        yield entry
                    contact_folders += 1
                elif cc == "IPF.Note":
                    # dossier mail (Réception, Envoyés, et TOUS les sous-dossiers/archives)
                    remaining = global_cap - mail_seen
                    if remaining <= 0:
                        logger.warning("EWS: plafond global atteint, arrêt lecture mails")
                        break
                    for entry in self._read_mail(folder, remaining):
                        mail_seen += 1
                        yielded += 1
                        yield entry
                    mail_folders += 1
                # autres classes (calendrier, tâches, notes, dossiers système) : ignorées
            except Exception as e:
                logger.error(f"EWS lecture dossier {getattr(folder,'name','?')} ({cc}): {e}")
        logger.info(f"EWS: {contact_folders} carnets + {mail_folders} dossiers mail parcourus "
                    f"-> {yielded} occurrences de contacts")

    def extract(self, global_cap: int = _DEFAULT_GLOBAL_CAP) -> List[Dict]:
        """Compat : matérialise toutes les occurrences en liste. À N'UTILISER que
        pour de petites boîtes/tests — préférez extract_iter() en production pour
        éviter la saturation mémoire sur les grosses boîtes (cf. incident 09/2026)."""
        return list(self.extract_iter(global_cap))
