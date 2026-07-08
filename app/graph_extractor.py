import httpx
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Optional, AsyncGenerator
import logging
from email.utils import parseaddr
import re

logger = logging.getLogger(__name__)

class GraphExtractor:
    """Extracteur de contacts via Microsoft Graph API"""
    
    def __init__(self, access_token: str, light: bool = False):
        # light=True : extraction "carnet d'adresses" légère (en-têtes seuls, sans corps
        # d'email ni analyse de signature) -> beaucoup plus rapide et robuste (offre gratuite).
        self.light = light
        self.access_token = access_token
        self.base_url = "https://graph.microsoft.com/v1.0"
        self.headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        self.session = None
        self.owner_info = None  # Informations du propriétaire du compte
        self.owner_email = None
        self.owner_name = None
    
    async def __aenter__(self):
        self.session = httpx.AsyncClient(timeout=30.0)
        # Initialiser automatiquement les informations du propriétaire
        await self._initialize_owner_info()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.aclose()
    
    async def get_user_info(self) -> Dict:
        """Récupérer les informations de l'utilisateur connecté"""
        response = await self.session.get(f"{self.base_url}/me", headers=self.headers)
        response.raise_for_status()
        return response.json()
    
    async def _initialize_owner_info(self):
        """Initialiser les informations du propriétaire du compte"""
        try:
            self.owner_info = await self.get_user_info()
            self.owner_email = self.owner_info.get('mail') or self.owner_info.get('userPrincipalName', '').lower()
            self.owner_name = self.owner_info.get('displayName', '')
            
            logger.info(f"Propriétaire identifié: {self.owner_name} ({self.owner_email})")
            
        except Exception as e:
            logger.error(f"Erreur lors de l'initialisation du propriétaire: {e}")
            self.owner_email = None
            self.owner_name = None
    
    async def extract_contacts(self, period_months: float = 1) -> AsyncGenerator[Dict, None]:
        """
        Extraire tous les contacts des emails sur une période donnée
        
        Args:
            period_months: Nombre de mois à extraire (1 par défaut, 0.25 = 1 semaine)
            
        Yields:
            Dict: Informations de contact extraites
        """
        
        # Calculer la date de début
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=period_months * 30)
        
        logger.info(f"Extraction des contacts du {start_date} au {end_date}")
        
        # Parcours de TOUS les dossiers ET de leurs sous-dossiers (récursif).
        # NB: /me/messages s'est révélé incomplet sur certaines boîtes (ne renvoyait
        # que quelques messages) -> on interroge chaque dossier, ce qui est fiable.
        root_folders = await self._get_mail_folders()
        logger.info(f"{len(root_folders)} dossiers racine détectés")
        for folder in root_folders:
            async for contact in self._walk_folder(folder, start_date, end_date):
                yield contact
    
    async def _walk_folder(self, folder: Dict, start_date: datetime, end_date: datetime) -> AsyncGenerator[Dict, None]:
        """Extraire un dossier puis, récursivement, tous ses sous-dossiers."""
        display = folder.get('displayName') or '?'
        total = folder.get('totalItemCount', '?')
        children = folder.get('childFolderCount', 0)
        logger.info(f"DOSSIER '{display}' : {total} items au total, {children} sous-dossiers")
        name = display.lower()
        skip = ('deleted items', 'éléments supprimés', 'drafts', 'brouillons')
        if name not in skip:
            async for contact in self._extract_from_folder(folder['id'], start_date, end_date):
                yield contact
        # Descendre dans les sous-dossiers (récursif)
        if folder.get('childFolderCount', 0):
            for child in await self._get_child_folders(folder['id']):
                async for contact in self._walk_folder(child, start_date, end_date):
                    yield contact

    async def _get_mail_folders(self) -> List[Dict]:
        """Récupérer TOUS les dossiers de premier niveau (avec nb de sous-dossiers)."""
        try:
            response = await self.session.get(
                f"{self.base_url}/me/mailFolders",
                headers=self.headers,
                params={'$top': 200, '$select': 'id,displayName,childFolderCount,totalItemCount'}
            )
            response.raise_for_status()
            return response.json().get('value', [])
        except Exception as e:
            logger.error(f"Erreur lors de la récupération des dossiers: {e}")
            return []

    async def _get_child_folders(self, folder_id: str) -> List[Dict]:
        """Récupérer les sous-dossiers directs d'un dossier."""
        try:
            response = await self.session.get(
                f"{self.base_url}/me/mailFolders/{folder_id}/childFolders",
                headers=self.headers,
                params={'$top': 200, '$select': 'id,displayName,childFolderCount,totalItemCount'}
            )
            response.raise_for_status()
            return response.json().get('value', [])
        except Exception as e:
            logger.error(f"Erreur sous-dossiers {folder_id}: {e}")
            return []
    
    async def _extract_from_folder(self, folder_id: str, start_date: datetime, end_date: datetime) -> AsyncGenerator[Dict, None]:
        """
        Extraire les contacts d'un dossier spécifique
        
        Args:
            folder_id: ID ou nom du dossier ('inbox', 'sentitems', etc.)
            start_date: Date de début
            end_date: Date de fin
            
        Yields:
            Dict: Contact extrait
        """
        
        # Construire le filtre de date
        start_iso = start_date.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        end_iso = end_date.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        
        # Champs récupérés : en mode léger on NE télécharge PAS le corps des mails
        # (le corps servait uniquement à l'analyse de signature, lourde en mémoire).
        select_fields = 'id,subject,from,toRecipients,ccRecipients,bccRecipients,receivedDateTime'
        if not self.light:
            select_fields += ',body'

        # Paramètres de la requête
        params = {
            '$filter': f"receivedDateTime ge {start_iso} and receivedDateTime le {end_iso}",
            '$select': select_fields,
            '$top': 100  # Traiter par batch de 100 (moins d'allers-retours)
        }
        
        next_url = f"{self.base_url}/me/mailFolders/{folder_id}/messages"
        
        while next_url:
            try:
                response = await self.session.get(next_url, headers=self.headers, params=params if '?' not in next_url else None)
                response.raise_for_status()
                data = response.json()
                
                messages = data.get('value', [])
                logger.info(f"Traitement de {len(messages)} emails du dossier {folder_id}")
                
                for message in messages:
                    # Extraire les contacts de cet email
                    async for contact in self._extract_contacts_from_message(message, folder_id):
                        yield contact
                
                # Pagination
                next_url = data.get('@odata.nextLink')
                params = None  # Les paramètres sont inclus dans nextLink
                
                # Pause pour éviter le throttling
                await asyncio.sleep(0.1)
                
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:  # Too Many Requests
                    retry_after = int(e.response.headers.get('Retry-After', 60))
                    logger.warning(f"Rate limited, attente de {retry_after} secondes")
                    await asyncio.sleep(retry_after)
                    continue
                else:
                    logger.error(f"Erreur HTTP lors de l'extraction: {e}")
                    break
            except Exception as e:
                logger.error(f"Erreur lors de l'extraction du dossier {folder_id}: {e}")
                break
    
    async def _extract_contacts_from_message(self, message: Dict, folder_type: str) -> AsyncGenerator[Dict, None]:
        """
        Extraire les contacts d'un message spécifique
        
        Args:
            message: Données du message depuis Graph API
            folder_type: Type de dossier ('inbox', 'sentitems', etc.)
            
        Yields:
            Dict: Contact extrait avec ses informations
        """
        
        message_id = message.get('id')
        received_date = message.get('receivedDateTime')
        body_content = message.get('body', {}).get('content', '')
        
        # Extraire l'expéditeur (From)
        sender = message.get('from')
        if sender and sender.get('emailAddress'):
            contact_info = self._parse_contact_info(
                sender['emailAddress'],
                body_content,
                'sender',
                message_id,
                received_date
            )
            if contact_info:
                yield contact_info
        
        # Extraire les destinataires (To, CC, BCC)
        for recipient_type in ['toRecipients', 'ccRecipients', 'bccRecipients']:
            recipients = message.get(recipient_type, [])
            for recipient in recipients:
                if recipient.get('emailAddress'):
                    contact_info = self._parse_contact_info(
                        recipient['emailAddress'],
                        body_content,
                        'recipient',
                        message_id,
                        received_date
                    )
                    if contact_info:
                        yield contact_info
    
    def _parse_contact_info(self, email_data: Dict, body_content: str, contact_type: str, message_id: str, received_date: str) -> Optional[Dict]:
        """
        Parser les informations d'un contact
        
        Args:
            email_data: Données email depuis Graph API
            body_content: Contenu du corps de l'email
            contact_type: Type de contact ('sender' ou 'recipient')
            message_id: ID du message source
            received_date: Date de réception
            
        Returns:
            Dict: Informations du contact parsées
        """
        
        email = email_data.get('address', '').lower().strip()
        display_name = email_data.get('name', '').strip()
        
        # Validation stricte de l'email
        if not email or email == '' or not self._is_valid_email(email):
            logger.warning(f"Email invalide ou manquant: {email_data}")
            return None
        
        # NOUVEAU: Exclure le propriétaire du compte
        if self.owner_email and email.lower() == self.owner_email.lower():
            logger.debug(f"Email du propriétaire exclu: {email}")
            return None
        
        # Exclure certains domaines systèmes
        excluded_domains = ['noreply', 'no-reply', 'donotreply', 'mailer-daemon', 'postmaster']
        if any(domain in email.lower() for domain in excluded_domains):
            logger.debug(f"Email exclu (domaine système): {email}")
            return None
        
        # Parser le nom complet
        nom, prenom = self._parse_name(display_name)
        
        # Extraire signature et informations additionnelles du corps de l'email
        signature_info = self._extract_signature_info(body_content, email, display_name)
        
        # Validation finale - s'assurer que l'email n'est jamais None
        final_email = email if email and email.strip() else None
        if not final_email:
            logger.warning(f"Email final invalide pour {display_name}: {email}")
            return None
        
        return {
            'email': final_email,
            'nom': nom,
            'prenom': prenom,
            'nom_complet': display_name,
            'intitule': signature_info.get('title'),
            'site_web': signature_info.get('website'),
            'signature_complete': signature_info.get('signature'),
            'source_email_id': message_id,
            'type_contact': contact_type,
            'date_contact': (datetime.fromisoformat(received_date.replace('Z', '+00:00')).replace(tzinfo=None) if received_date else datetime.utcnow())
        }
    
    def _parse_name(self, display_name: str) -> tuple[Optional[str], Optional[str]]:
        """
        Parser un nom complet en nom et prénom
        
        Args:
            display_name: Nom complet à parser
            
        Returns:
            tuple: (nom, prenom)
        """
        
        if not display_name:
            return None, None
        
        # Nettoyer le nom
        name = display_name.strip()
        
        # Supprimer les caractères indésirables
        name = re.sub(r'[<>()[\]{}"]', '', name)
        
        # Si c'est un email, l'ignorer
        if '@' in name:
            return None, None
        
        # Séparer par espaces
        parts = name.split()
        
        if len(parts) == 0:
            return None, None
        elif len(parts) == 1:
            # Un seul mot, considérer comme nom ou prénom selon le contexte
            return parts[0], None
        elif len(parts) == 2:
            # Prénom Nom
            return parts[1], parts[0]
        else:
            # Plus de 2 mots, prendre le premier comme prénom et le reste comme nom
            prenom = parts[0]
            nom = ' '.join(parts[1:])
            return nom, prenom
    
    def _extract_signature_info(self, body_content: str, email: str, display_name: str = "") -> Dict:
        """
        Extraire les informations de signature d'un email avec l'extracteur intelligent
        
        Args:
            body_content: Contenu HTML/texte de l'email
            email: Adresse email du contact
            display_name: Nom d'affichage du contact
            
        Returns:
            Dict: Informations extraites (title, website, signature)
        """
        
        if not body_content:
            return {}
        
        # Vérifier si la signature appartient au propriétaire du compte
        if self._is_owner_signature(body_content, email, display_name):
            logger.debug(f"Signature du propriétaire détectée et exclue pour {email}")
            return {}
        
        # Utiliser le nouvel extracteur intelligent
        from app.smart_signature_extractor import SmartSignatureExtractor
        
        extractor = SmartSignatureExtractor()
        professional_info = extractor.extract_professional_info(body_content, email)
        
        # Mapper vers le format attendu
        result = {}
        if professional_info.get('title'):
            result['title'] = professional_info['title']
        if professional_info.get('website'):
            result['website'] = professional_info['website']
        if professional_info.get('linkedin'):
            result['linkedin'] = professional_info['linkedin']
        if professional_info.get('signature'):
            result['signature'] = professional_info['signature']
        
        # Log de la qualité d'extraction pour debugging
        if professional_info:
            quality_scores = extractor.analyze_extraction_quality(professional_info)
            if any(score < 0.5 for score in quality_scores.values()):
                logger.debug(f"Qualité d'extraction faible pour {email}: {quality_scores}")
        
        return result
    
    def _is_owner_signature(self, body_content: str, email: str, display_name: str) -> bool:
        """
        Détecter si une signature appartient au propriétaire du compte
        
        Args:
            body_content: Contenu de l'email
            email: Email du contact
            display_name: Nom d'affichage du contact
            
        Returns:
            bool: True si c'est la signature du propriétaire
        """
        
        if not self.owner_name or not self.owner_email:
            return False
        
        # Convertir en minuscules pour comparaison
        body_lower = body_content.lower()
        owner_name_lower = self.owner_name.lower()
        owner_email_lower = self.owner_email.lower()
        display_name_lower = display_name.lower()
        
        # 1. Vérifier si l'email du propriétaire apparaît dans la signature
        if owner_email_lower in body_lower:
            logger.debug(f"Email propriétaire trouvé dans signature: {self.owner_email}")
            return True
        
        # 2. Vérifier si le nom du propriétaire apparaît dans la signature
        if owner_name_lower and len(owner_name_lower.strip()) > 2:
            # Séparer le nom en parties pour une détection plus flexible
            owner_name_parts = owner_name_lower.split()
            if len(owner_name_parts) >= 2:
                # Vérifier si au moins 2 parties du nom sont présentes
                found_parts = sum(1 for part in owner_name_parts if len(part) > 2 and part in body_lower)
                if found_parts >= 2:
                    logger.debug(f"Nom propriétaire trouvé dans signature: {self.owner_name}")
                    return True
        
        # 3. Vérifier si le display_name correspond au propriétaire
        if display_name_lower and owner_name_lower:
            # Calculer la similarité entre les noms
            from difflib import SequenceMatcher
            similarity = SequenceMatcher(None, display_name_lower, owner_name_lower).ratio()
            if similarity > 0.8:  # 80% de similarité
                logger.debug(f"Nom similaire détecté: {display_name} ≈ {self.owner_name}")
                return True
        
        # 4. Détecter les patterns typiques d'auto-signature (emails envoyés)
        auto_signature_patterns = [
            r'sent from my iphone',
            r'sent from my ipad', 
            r'sent from my samsung',
            r'envoyé depuis mon iphone',
            r'envoyé depuis mon ipad',
            r'get outlook for',
            r'obtenir outlook pour'
        ]
        
        for pattern in auto_signature_patterns:
            if re.search(pattern, body_lower):
                logger.debug(f"Pattern auto-signature détecté: {pattern}")
                return True
        
        return False
    
    def _is_valid_email(self, email: str) -> bool:
        """Valider une adresse email"""
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return re.match(pattern, email) is not None