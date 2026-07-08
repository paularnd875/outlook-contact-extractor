from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, create_engine, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from datetime import datetime
import os
from typing import Optional

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./contacts.db")

# Pour SQLite asynchrone
if DATABASE_URL.startswith("sqlite"):
    DATABASE_URL = DATABASE_URL.replace("sqlite://", "sqlite+aiosqlite://")

engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, class_=AsyncSession)

Base = declarative_base()

class Contact(Base):
    """Modèle de données pour les contacts"""
    __tablename__ = "contacts"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, index=True, nullable=False)
    nom = Column(String, nullable=True)
    prenom = Column(String, nullable=True)
    nom_complet = Column(String, nullable=True)
    nom_normalise = Column(String, index=True, nullable=True)
    intitule = Column(String, nullable=True)
    site_web = Column(String, nullable=True)
    linkedin_url = Column(String, nullable=True)
    signature_complete = Column(Text, nullable=True)
    source_email_id = Column(String, nullable=True)  # ID de l'email source
    date_premier_contact = Column(DateTime, default=datetime.utcnow)
    date_dernier_contact = Column(DateTime, default=datetime.utcnow)
    nombre_emails = Column(Integer, default=1)
    type_contact = Column(String, default="unknown")  # "sender", "recipient", "both"
    valide = Column(Boolean, default=True)  # Pour validation manuelle
    session_id = Column(String, nullable=True, index=True)  # Pour isoler les sessions
    
    # Colonnes pour classification IA
    classification = Column(String, nullable=True)  # "client", "prospect", "avocat", "autre"
    justification_classification = Column(Text, nullable=True)  # Explication de l'IA
    confiance_classification = Column(Integer, nullable=True)  # Score 0-100
    
    # Contrainte unique: même email autorisé dans différentes sessions
    __table_args__ = (UniqueConstraint('email', 'session_id', name='unique_email_per_session'),)
    
    def to_dict(self):
        """Convertir en dictionnaire pour l'API"""
        return {
            "id": self.id,
            "email": self.email,
            "nom": self.nom,
            "prenom": self.prenom,
            "nom_complet": self.nom_complet,
            "nom_normalise": self.nom_normalise,
            "intitule": self.intitule,
            "site_web": self.site_web,
            "linkedin_url": self.linkedin_url,
            "date_premier_contact": self.date_premier_contact.isoformat() if self.date_premier_contact else None,
            "date_dernier_contact": self.date_dernier_contact.isoformat() if self.date_dernier_contact else None,
            "nombre_emails": self.nombre_emails,
            "type_contact": self.type_contact,
            "valide": self.valide,
            "classification": self.classification,
            "justification_classification": self.justification_classification,
            "confiance_classification": self.confiance_classification
        }

class ExtractionSession(Base):
    """Modèle pour suivre les sessions d'extraction"""
    __tablename__ = "extraction_sessions"
    
    id = Column(String, primary_key=True)  # UUID de session
    user_id = Column(String, nullable=False)
    email_address = Column(String, nullable=False)  # Adresse email extraite
    date_debut = Column(DateTime, default=datetime.utcnow)
    date_fin = Column(DateTime, nullable=True)
    status = Column(String, default="in_progress")  # in_progress, completed, error
    total_emails = Column(Integer, default=0)  # Nombre d'emails traités
    total_contacts = Column(Integer, default=0)  # Nombre de contacts trouvés
    estimated_total_emails = Column(Integer, default=0)  # Estimation du total d'emails à traiter
    processed_folders = Column(Integer, default=0)  # Nombre de dossiers traités
    total_folders = Column(Integer, default=0)  # Nombre total de dossiers
    current_step = Column(String, default="")  # Étape actuelle
    periode_debut = Column(DateTime, nullable=True)
    periode_fin = Column(DateTime, nullable=True)
    erreur_message = Column(Text, nullable=True)
    
    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "email_address": self.email_address,
            "date_debut": self.date_debut.isoformat() if self.date_debut else None,
            "date_fin": self.date_fin.isoformat() if self.date_fin else None,
            "status": self.status,
            "total_emails": self.total_emails,
            "total_contacts": self.total_contacts,
            "estimated_total_emails": self.estimated_total_emails,
            "processed_folders": self.processed_folders,
            "total_folders": self.total_folders,
            "current_step": self.current_step,
            "periode_debut": self.periode_debut.isoformat() if self.periode_debut else None,
            "periode_fin": self.periode_fin.isoformat() if self.periode_fin else None
        }

async def init_db():
    """Initialiser la base de données"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_db():
    """Obtenir une session de base de données"""
    async with SessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

# Fonctions utilitaires pour la base de données
async def create_contact(db: AsyncSession, email: str, contact_data: dict, session_id: str) -> Contact:
    """Créer un nouveau contact"""
    # S'assurer que l'email est inclus dans les données
    full_data = {**contact_data, 'email': email, 'session_id': session_id}
    contact = Contact(**full_data)
    db.add(contact)
    await db.commit()
    await db.refresh(contact)
    return contact

async def update_contact(db: AsyncSession, contact: Contact, update_data: dict) -> Contact:
    """Mettre à jour un contact existant"""
    for key, value in update_data.items():
        if key in ("date_dernier_contact", "date_premier_contact"):
            continue  # dates gérées séparément (max / min des vraies dates d'emails)
        if hasattr(contact, key) and value is not None:
            setattr(contact, key, value)

    # Dates RÉELLES des échanges (et non l'heure d'extraction) :
    #   dernier échange = la plus récente vue ; premier échange = la plus ancienne.
    nd = update_data.get("date_dernier_contact")
    if nd and (not contact.date_dernier_contact or nd > contact.date_dernier_contact):
        contact.date_dernier_contact = nd
    npd = update_data.get("date_premier_contact")
    if npd and (not contact.date_premier_contact or npd < contact.date_premier_contact):
        contact.date_premier_contact = npd

    await db.commit()
    await db.refresh(contact)
    return contact

async def get_or_create_contact(db: AsyncSession, email: str, contact_data: dict, session_id: str) -> tuple[Contact, bool]:
    """Obtenir un contact existant ou en créer un nouveau"""
    from sqlalchemy import select
    
    # Chercher un contact existant dans la même session
    result = await db.execute(
        select(Contact).where(Contact.email == email, Contact.session_id == session_id)
    )
    existing_contact = result.scalar_one_or_none()
    
    if existing_contact:
        # Mettre à jour avec les nouvelles informations (garder les plus complètes)
        update_data = {}
        for key, value in contact_data.items():
            if key in ("date_dernier_contact", "date_premier_contact"):
                update_data[key] = value  # toujours transmis -> max/min appliqué dans update_contact
            elif value and (not getattr(existing_contact, key) or len(str(value)) > len(str(getattr(existing_contact, key) or ""))):
                update_data[key] = value
        
        if update_data:
            existing_contact = await update_contact(db, existing_contact, update_data)
        
        # Incrémenter le compteur d'emails
        existing_contact.nombre_emails += 1
        await db.commit()
        
        return existing_contact, False
    else:
        # Créer un nouveau contact
        new_contact = await create_contact(db, email, contact_data, session_id)
        return new_contact, True