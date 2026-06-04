from sqlalchemy.ext.asyncio import AsyncSession
from typing import Dict, Optional
import logging
from datetime import datetime

from app.database import get_or_create_contact, Contact
from app.normalizer import smart_normalize_contact

logger = logging.getLogger(__name__)

class ContactProcessor:
    """Processeur pour traiter et stocker les contacts extraits"""
    
    def __init__(self, db_session: AsyncSession, session_id: str):
        self.db = db_session
        self.session_id = session_id
        self.processed_count = 0
        self.created_count = 0
        self.updated_count = 0
    
    async def process_contact(self, contact_data: Dict) -> Optional[Contact]:
        """
        Traiter un contact extrait et le stocker en base
        
        Args:
            contact_data: Données brutes du contact depuis l'extracteur
            
        Returns:
            Contact: Le contact créé ou mis à jour
        """
        
        try:
            # Valider les données essentielles
            if not contact_data.get('email'):
                logger.warning("Contact ignoré: email manquant")
                return None
            
            # Normaliser le nom
            nom_normalise = smart_normalize_contact(
                contact_data.get('nom'),
                contact_data.get('prenom'), 
                contact_data.get('nom_complet')
            )
            
            # Préparer les données pour la base
            processed_data = {
                'nom': self._clean_string(contact_data.get('nom')),
                'prenom': self._clean_string(contact_data.get('prenom')),
                'nom_complet': self._clean_string(contact_data.get('nom_complet')),
                'nom_normalise': nom_normalise,
                'intitule': self._clean_string(contact_data.get('intitule')),
                'site_web': self._clean_url(contact_data.get('site_web')),
                'signature_complete': self._clean_string(contact_data.get('signature_complete'), max_length=1000),
                'source_email_id': contact_data.get('source_email_id'),
                'type_contact': contact_data.get('type_contact', 'unknown'),
                'date_dernier_contact': contact_data.get('date_contact', datetime.utcnow())
            }
            
            # Créer ou mettre à jour le contact
            contact, is_new = await get_or_create_contact(
                self.db, 
                contact_data['email'], 
                processed_data, 
                self.session_id
            )
            
            # Mettre à jour les compteurs
            self.processed_count += 1
            if is_new:
                self.created_count += 1
            else:
                self.updated_count += 1
            
            # Log périodique
            if self.processed_count % 100 == 0:
                logger.info(f"Contacts traités: {self.processed_count} (créés: {self.created_count}, mis à jour: {self.updated_count})")
            
            return contact
            
        except Exception as e:
            logger.error(f"Erreur lors du traitement du contact {contact_data.get('email', 'unknown')}: {e}")
            return None
    
    def _clean_string(self, value: Optional[str], max_length: int = 255) -> Optional[str]:
        """
        Nettoyer et valider une chaîne de caractères
        
        Args:
            value: Valeur à nettoyer
            max_length: Longueur maximale
            
        Returns:
            str: Chaîne nettoyée ou None
        """
        
        if not value:
            return None
        
        # Convertir en string et nettoyer
        cleaned = str(value).strip()
        
        # Supprimer les caractères de contrôle
        cleaned = ''.join(char for char in cleaned if ord(char) >= 32)
        
        # Tronquer si trop long
        if len(cleaned) > max_length:
            cleaned = cleaned[:max_length-3] + "..."
        
        return cleaned if cleaned else None
    
    def _clean_url(self, url: Optional[str]) -> Optional[str]:
        """
        Nettoyer et valider une URL
        
        Args:
            url: URL à nettoyer
            
        Returns:
            str: URL nettoyée ou None
        """
        
        if not url:
            return None
        
        cleaned = str(url).strip()
        
        # Ajouter https:// si pas de protocole
        if not cleaned.startswith(('http://', 'https://')):
            if cleaned.startswith('www.') or '.' in cleaned:
                cleaned = f"https://{cleaned}"
            else:
                return None
        
        # Validation basique d'URL
        if len(cleaned) > 500 or ' ' in cleaned:
            return None
        
        return cleaned
    
    async def finalize_processing(self) -> Dict:
        """
        Finaliser le traitement et retourner les statistiques
        
        Returns:
            Dict: Statistiques de traitement
        """
        
        try:
            await self.db.commit()
            
            stats = {
                'processed_count': self.processed_count,
                'created_count': self.created_count,
                'updated_count': self.updated_count,
                'session_id': self.session_id
            }
            
            logger.info(f"Traitement finalisé: {stats}")
            return stats
            
        except Exception as e:
            logger.error(f"Erreur lors de la finalisation: {e}")
            await self.db.rollback()
            raise
    
    async def deduplicate_contacts(self) -> int:
        """
        Dédupliquer les contacts dans la session courante
        
        Returns:
            int: Nombre de doublons supprimés
        """
        
        try:
            from sqlalchemy import select, func
            
            # Trouver les emails en doublon
            duplicate_query = select(Contact.email, func.count(Contact.id).label('count'))\
                .where(Contact.session_id == self.session_id)\
                .group_by(Contact.email)\
                .having(func.count(Contact.id) > 1)
            
            result = await self.db.execute(duplicate_query)
            duplicates = result.fetchall()
            
            removed_count = 0
            
            for email, count in duplicates:
                # Récupérer tous les contacts pour cet email
                contacts_query = select(Contact)\
                    .where(Contact.email == email, Contact.session_id == self.session_id)\
                    .order_by(Contact.nombre_emails.desc(), Contact.date_dernier_contact.desc())
                
                contacts_result = await self.db.execute(contacts_query)
                contacts = contacts_result.scalars().all()
                
                if len(contacts) > 1:
                    # Garder le premier (le plus complet), supprimer les autres
                    best_contact = contacts[0]
                    duplicates_to_remove = contacts[1:]
                    
                    # Fusionner les informations des doublons dans le meilleur contact
                    await self._merge_contact_info(best_contact, duplicates_to_remove)
                    
                    # Supprimer les doublons
                    for duplicate in duplicates_to_remove:
                        await self.db.delete(duplicate)
                        removed_count += 1
            
            await self.db.commit()
            logger.info(f"Déduplication terminée: {removed_count} doublons supprimés")
            
            return removed_count
            
        except Exception as e:
            logger.error(f"Erreur lors de la déduplication: {e}")
            await self.db.rollback()
            return 0
    
    async def _merge_contact_info(self, best_contact: Contact, duplicates: list[Contact]):
        """
        Fusionner les informations des contacts dupliqués dans le meilleur contact
        
        Args:
            best_contact: Contact à garder
            duplicates: Contacts à fusionner puis supprimer
        """
        
        # Fusionner les informations manquantes ou plus complètes
        for duplicate in duplicates:
            # Prendre les infos les plus complètes
            if not best_contact.nom and duplicate.nom:
                best_contact.nom = duplicate.nom
            elif duplicate.nom and len(duplicate.nom) > len(best_contact.nom or ""):
                best_contact.nom = duplicate.nom
            
            if not best_contact.prenom and duplicate.prenom:
                best_contact.prenom = duplicate.prenom
            elif duplicate.prenom and len(duplicate.prenom) > len(best_contact.prenom or ""):
                best_contact.prenom = duplicate.prenom
            
            if not best_contact.nom_complet and duplicate.nom_complet:
                best_contact.nom_complet = duplicate.nom_complet
            elif duplicate.nom_complet and len(duplicate.nom_complet) > len(best_contact.nom_complet or ""):
                best_contact.nom_complet = duplicate.nom_complet
            
            if not best_contact.intitule and duplicate.intitule:
                best_contact.intitule = duplicate.intitule
            
            if not best_contact.site_web and duplicate.site_web:
                best_contact.site_web = duplicate.site_web
            
            # Mettre à jour les dates
            if duplicate.date_premier_contact and (not best_contact.date_premier_contact or duplicate.date_premier_contact < best_contact.date_premier_contact):
                best_contact.date_premier_contact = duplicate.date_premier_contact
            
            if duplicate.date_dernier_contact and (not best_contact.date_dernier_contact or duplicate.date_dernier_contact > best_contact.date_dernier_contact):
                best_contact.date_dernier_contact = duplicate.date_dernier_contact
            
            # Additionner le nombre d'emails
            best_contact.nombre_emails += duplicate.nombre_emails
            
            # Mettre à jour le type de contact
            if best_contact.type_contact != duplicate.type_contact:
                if best_contact.type_contact == 'sender' and duplicate.type_contact == 'recipient':
                    best_contact.type_contact = 'both'
                elif best_contact.type_contact == 'recipient' and duplicate.type_contact == 'sender':
                    best_contact.type_contact = 'both'
        
        # Recalculer la normalisation avec les nouvelles infos
        best_contact.nom_normalise = smart_normalize_contact(
            best_contact.nom,
            best_contact.prenom,
            best_contact.nom_complet
        )