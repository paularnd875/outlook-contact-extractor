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
    
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.base_url = "https://graph.microsoft.com/v1.0"
        self.headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }
        self.session = None
    
    async def __aenter__(self):
        self.session = httpx.AsyncClient(timeout=30.0)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.aclose()
    
    async def get_user_info(self) -> Dict:
        """Récupérer les informations de l'utilisateur connecté"""
        response = await self.session.get(f"{self.base_url}/me", headers=self.headers)
        response.raise_for_status()
        return response.json()
    
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
        
        # Extraire les emails reçus et envoyés
        async for contact in self._extract_from_folder("inbox", start_date, end_date):
            yield contact
            
        async for contact in self._extract_from_folder("sentitems", start_date, end_date):
            yield contact
            
        # Traiter d'autres dossiers si nécessaire
        folders = await self._get_mail_folders()
        for folder in folders:
            if folder['displayName'].lower() not in ['inbox', 'sent items', 'deleted items', 'drafts']:
                async for contact in self._extract_from_folder(folder['id'], start_date, end_date):
                    yield contact
    
    async def _get_mail_folders(self) -> List[Dict]:
        """Récupérer la liste des dossiers de messagerie"""
        try:
            response = await self.session.get(
                f"{self.base_url}/me/mailFolders",
                headers=self.headers
            )
            response.raise_for_status()
            return response.json().get('value', [])
        except Exception as e:
            logger.error(f"Erreur lors de la récupération des dossiers: {e}")
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
        
        # Paramètres de la requête
        params = {
            '$filter': f"receivedDateTime ge {start_iso} and receivedDateTime le {end_iso}",
            '$select': 'id,subject,from,toRecipients,ccRecipients,bccRecipients,receivedDateTime,body',
            '$top': 50  # Traiter par batch de 50
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
        
        # Exclure certains domaines systèmes
        excluded_domains = ['noreply', 'no-reply', 'donotreply', 'mailer-daemon', 'postmaster']
        if any(domain in email.lower() for domain in excluded_domains):
            logger.debug(f"Email exclu (domaine système): {email}")
            return None
        
        # Parser le nom complet
        nom, prenom = self._parse_name(display_name)
        
        # Extraire signature et informations additionnelles du corps de l'email
        signature_info = self._extract_signature_info(body_content, email)
        
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
            'date_contact': datetime.fromisoformat(received_date.replace('Z', '+00:00')) if received_date else datetime.utcnow()
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
    
    def _extract_signature_info(self, body_content: str, email: str) -> Dict:
        """
        Extraire les informations de signature d'un email
        
        Args:
            body_content: Contenu HTML/texte de l'email
            email: Adresse email du contact
            
        Returns:
            Dict: Informations extraites (title, website, signature)
        """
        
        if not body_content:
            return {}
        
        # Nettoyer le HTML d'abord
        clean_text = re.sub(r'<[^>]+>', ' ', body_content)
        clean_text = re.sub(r'&nbsp;', ' ', clean_text)
        clean_text = re.sub(r'&[a-zA-Z0-9#]+;', ' ', clean_text)  # Enlever entités HTML
        clean_text = ' '.join(clean_text.split())  # Normaliser espaces
        
        result = {}
        
        # Indicateurs de contenu marketing/spam à éviter
        spam_indicators = [
            'partnership', 'collaborate', 'meeting', 'discuss', 'opportunity',
            'proposal', 'enhance', 'develop', 'promote', 'market', 'debug',
            'margins', 'revenue', 'innovative', 'cutting-edge', 'strategies',
            'Dear Partner', 'Business Development Manager', 'when can I call',
            'would be a good time', 'available for', 'interested in',
            'promptly', 'significantly', 'transparently', 'judiciously',
            'fungibly', 'hyperscale', 'omnichann', 'bottom-up', 'front end',
            'resource-maximizing', 'developmentally appropriate', 'principle centered'
        ]
        
        # Compter les indicateurs de spam dans le texte complet
        spam_count = sum(1 for indicator in spam_indicators if indicator.lower() in clean_text.lower())
        
        # Si le contenu est très spammy, ne pas extraire d'informations
        if spam_count > 5:
            logger.debug(f"Contenu trop spammy ({spam_count} indicateurs), ignoré")
            return {}
        
        # Patterns pour les titres/fonctions professionnels (plus précis)
        title_patterns = [
            # Titres juridiques exacts
            r'(?i)(?:^|\s|-)(?:maître|avocat[e]?|counsel|attorney|lawyer|barrister|solicitor)(?:\s|$|-)',
            # Fonctions direction exactes
            r'(?i)(?:^|\s)(?:directeur|directrice|director|président|présidente|president|ceo|cto|cfo|managing director|gérant)(?:\s|$)',
            # Fonctions commerciales exactes
            r'(?i)(?:^|\s)(?:manager|responsable|chef|partner|associé|fondateur|founder)(?:\s|$)',
            # Fonctions techniques exactes
            r'(?i)(?:^|\s)(?:consultant|expert|specialist|analyst|developer|engineer|architecte?)(?:\s|$)',
        ]
        
        # Rechercher les titres en évitant le contenu marketing
        for pattern in title_patterns:
            matches = re.finditer(pattern, clean_text)
            for match in matches:
                # Extraire un contexte limité autour du titre
                start = max(0, match.start() - 30)
                end = min(len(clean_text), match.end() + 30)
                context = clean_text[start:end].strip()
                
                # Vérifier que ce contexte n'est pas marketing
                context_spam_count = sum(1 for indicator in spam_indicators if indicator.lower() in context.lower())
                
                if context_spam_count == 0 and len(context) < 100:
                    # Extraire juste la partie pertinente
                    words = context.split()
                    title_words = []
                    
                    # Trouver le mot-clé du titre et prendre quelques mots autour
                    for i, word in enumerate(words):
                        if re.search(pattern, word, re.IGNORECASE):
                            # Prendre le mot du titre et quelques mots avant/après
                            start_idx = max(0, i - 2)
                            end_idx = min(len(words), i + 3)
                            title_words = words[start_idx:end_idx]
                            break
                    
                    if title_words:
                        clean_title = ' '.join(title_words).strip()
                        # Filtrer les titres trop longs ou suspects
                        if len(clean_title) < 50 and not any(spam in clean_title.lower() for spam in spam_indicators):
                            result['title'] = clean_title
                            break
            
            if result.get('title'):
                break
        
        # Si pas de titre trouvé, chercher dans les lignes courtes de fin d'email (signatures)
        if not result.get('title'):
            lines = clean_text.split('\n')
            # Prendre les dernières lignes (signatures typiques)
            for line in lines[-10:]:
                line = line.strip()
                if (len(line) < 80 and 
                    any(title_word in line.lower() for title_word in 
                        ['directeur', 'manager', 'avocat', 'consultant', 'président', 'responsable', 'chef', 'partner']) and
                    not any(spam in line.lower() for spam in spam_indicators)):
                    result['title'] = line
                    break
        
        # Extraire les sites web (amélioré)
        website_pattern = r'https?://(?!(?:unsubscribe|tracking|pixel|analytics))[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*'
        website_matches = re.findall(website_pattern, clean_text)
        
        if website_matches:
            for website in website_matches:
                if not any(exclude in website.lower() for exclude in 
                          ['unsubscribe', 'tracking', 'pixel', 'analytics', 'marketing', 'substack']):
                    result['website'] = website
                    break
        
        # Stocker une signature nettoyée très limitée
        if clean_text and spam_count < 3:
            # Prendre juste les premières lignes non-marketing
            signature_lines = []
            for line in clean_text.split('\n')[:5]:
                line = line.strip()
                if (line and len(line) < 100 and 
                    not any(spam in line.lower() for spam in spam_indicators)):
                    signature_lines.append(line)
                    if len(signature_lines) >= 3:
                        break
            
            if signature_lines:
                result['signature'] = ' | '.join(signature_lines)[:200]
        
        return result
    
    def _is_valid_email(self, email: str) -> bool:
        """Valider une adresse email"""
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return re.match(pattern, email) is not None