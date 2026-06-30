import re
from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

class SmartSignatureExtractor:
    """
    Extracteur intelligent de signatures professionnelles
    Conçu pour récupérer des intitulés cohérents et des informations professionnelles réelles
    """
    
    def __init__(self):
        # Titres professionnels juridiques et business (français/anglais)
        self.professional_titles = {
            'juridique': [
                'avocat', 'avocate', 'maître', 'counsel', 'attorney', 'lawyer', 'barrister', 'solicitor',
                'juriste', 'notaire', 'huissier', 'magistrat', 'procureur', 'substitut'
            ],
            'direction': [
                'directeur', 'directrice', 'director', 'président', 'présidente', 'president',
                'ceo', 'cto', 'cfo', 'managing director', 'gérant', 'gérantе', 'manager'
            ],
            'commercial': [
                'responsable', 'chef', 'head', 'manager', 'partner', 'associé', 'associée',
                'fondateur', 'fondatrice', 'founder', 'co-founder', 'business development'
            ],
            'technique': [
                'consultant', 'consultante', 'expert', 'experte', 'specialist', 'analyste',
                'analyst', 'developer', 'engineer', 'architecte', 'senior', 'lead'
            ]
        }
        
        # Mots-clés de spam à éviter absolument
        self.spam_keywords = [
            'partnership', 'collaborate', 'opportunity', 'proposal', 'enhance', 'develop',
            'promote', 'market', 'margins', 'revenue', 'innovative', 'cutting-edge',
            'strategies', 'dear partner', 'business development manager', 'when can i call',
            'available for', 'interested in', 'promptly', 'significantly', 'transparently',
            'debug', 'fungibly', 'hyperscale', 'omnichann', 'bottom-up', 'front end',
            'resource-maximizing', 'leverage', 'synergies', 'paradigm', 'disruptive',
            'game-changer', 'best practices', 'core competencies', 'value proposition'
        ]
        
        # Indicateurs de structure de signature
        self.signature_indicators = [
            '---', '--', '___', '___', 'cordialement', 'cordially', 'best regards',
            'regards', 'sincerely', 'kind regards', 'yours sincerely', 'bien à vous',
            'sent from', 'envoyé de', 'this email', 'confidential', 'confidentiel'
        ]
    
    def extract_professional_info(self, email_body: str, sender_email: str, context_info: Dict = None) -> Dict[str, Optional[str]]:
        """
        Extraire les informations professionnelles d'un email avec détection contextuelle
        
        Args:
            email_body: Corps complet de l'email
            sender_email: Email de l'expéditeur
            context_info: Informations contextuelles (domaine, type d'email, etc.)
            
        Returns:
            Dict: {title, website, linkedin, company, phone, signature}
        """
        
        if not email_body:
            return {}
        
        # Analyser le contexte pour adapter la stratégie d'extraction
        context = self._analyze_context(email_body, sender_email, context_info or {})
        
        # Nettoyer le contenu HTML
        clean_content = self._clean_html(email_body)
        
        # Identifier la signature réelle avec détection contextuelle
        signature_content = self._extract_signature_block_contextual(clean_content, context)
        
        # Si pas de signature identifiable, utiliser stratégie adaptative
        if not signature_content:
            signature_content = self._fallback_signature_detection(clean_content, context)
        
        # Extraire les informations de la signature avec contexte
        result = self._extract_contextual_info(signature_content, sender_email, context)
        
        return {k: v for k, v in result.items() if v}
    
    def _clean_html(self, content: str) -> str:
        """Nettoyer le contenu HTML et normaliser le texte en préservant la structure"""
        
        if not content:
            return ""
        
        # Convertir certaines balises HTML en sauts de ligne
        clean = content
        
        # Remplacer les balises de saut par des sauts de ligne
        line_break_tags = ['<br>', '<br/>', '<br />', '</p>', '</div>', '</tr>', '</td>']
        for tag in line_break_tags:
            clean = clean.replace(tag, '\n')
        
        # Supprimer toutes les autres balises HTML
        clean = re.sub(r'<[^>]+>', ' ', clean)
        
        # Décoder les entités HTML
        html_entities = {
            '&nbsp;': ' ', '&amp;': '&', '&lt;': '<', '&gt;': '>',
            '&quot;': '"', '&#39;': "'", '&copy;': '©', '&reg;': '®'
        }
        
        for entity, replacement in html_entities.items():
            clean = clean.replace(entity, replacement)
        
        # Normaliser les sauts de ligne multiples
        clean = re.sub(r'\n\s*\n', '\n', clean)
        
        # Nettoyer les espaces en début/fin de chaque ligne
        lines = []
        for line in clean.split('\n'):
            line = line.strip()
            if line:  # Garder seulement les lignes non vides
                lines.append(line)
        
        return '\n'.join(lines)
    
    def _analyze_context(self, email_body: str, sender_email: str, context_info: Dict) -> Dict:
        """
        Analyser le contexte de l'email pour adapter la stratégie d'extraction
        """
        
        context = {
            'domain_type': self._classify_domain(sender_email),
            'email_language': self._detect_language(email_body),
            'email_type': self._classify_email_type(email_body),
            'formality_level': self._assess_formality(email_body),
            'likely_profession': self._infer_profession(sender_email, email_body)
        }
        
        # Ajouter les informations contextuelles fournies
        context.update(context_info)
        
        logger.debug(f"Contexte analysé pour {sender_email}: {context}")
        return context
    
    def _classify_domain(self, email: str) -> str:
        """Classifier le type de domaine de l'email"""
        
        domain = email.split('@')[-1].lower()
        
        if any(provider in domain for provider in ['gmail', 'hotmail', 'outlook', 'yahoo', 'live']):
            return 'personal'
        elif any(legal in domain for legal in ['avocat', 'barreau', 'cabinet', 'legal']):
            return 'legal'
        elif any(corp in domain for corp in ['consulting', 'conseil', 'group', 'sa', 'sarl']):
            return 'corporate'
        else:
            return 'professional'
    
    def _detect_language(self, content: str) -> str:
        """Détecter la langue principale de l'email"""
        
        french_indicators = ['cordialement', 'bonjour', 'madame', 'monsieur', 'société', 'cabinet']
        english_indicators = ['regards', 'dear', 'company', 'corporation', 'sincerely']
        
        french_count = sum(1 for word in french_indicators if word in content.lower())
        english_count = sum(1 for word in english_indicators if word in content.lower())
        
        return 'french' if french_count > english_count else 'english'
    
    def _classify_email_type(self, content: str) -> str:
        """Classifier le type d'email (business, marketing, legal, etc.)"""
        
        marketing_keywords = ['opportunity', 'partnership', 'promote', 'offer', 'deal']
        legal_keywords = ['dossier', 'juridique', 'legal', 'court', 'tribunal']
        business_keywords = ['meeting', 'réunion', 'project', 'projet', 'collaboration']
        
        content_lower = content.lower()
        
        if sum(1 for kw in marketing_keywords if kw in content_lower) > 2:
            return 'marketing'
        elif sum(1 for kw in legal_keywords if kw in content_lower) > 1:
            return 'legal'
        elif sum(1 for kw in business_keywords if kw in content_lower) > 1:
            return 'business'
        else:
            return 'general'
    
    def _assess_formality(self, content: str) -> str:
        """Évaluer le niveau de formalité de l'email"""
        
        formal_indicators = ['madame', 'monsieur', 'veuillez agréer', 'distinguished', 'respectfully']
        informal_indicators = ['salut', 'hey', 'bonjour', 'hi', 'cheers']
        
        content_lower = content.lower()
        
        formal_score = sum(1 for indicator in formal_indicators if indicator in content_lower)
        informal_score = sum(1 for indicator in informal_indicators if indicator in content_lower)
        
        if formal_score > informal_score:
            return 'formal'
        elif informal_score > formal_score:
            return 'informal'
        else:
            return 'neutral'
    
    def _infer_profession(self, email: str, content: str) -> str:
        """Inférer la profession probable basée sur l'email et le contenu"""
        
        domain = email.split('@')[-1].lower()
        content_lower = content.lower()
        
        if ('avocat' in domain or 'barreau' in domain or 
            sum(1 for word in ['avocat', 'maître', 'juridique'] if word in content_lower) > 0):
            return 'legal'
        elif any(word in content_lower for word in ['consultant', 'consulting', 'conseil']):
            return 'consulting'
        elif any(word in content_lower for word in ['directeur', 'ceo', 'president']):
            return 'executive'
        else:
            return 'unknown'
    
    def _extract_signature_block_contextual(self, content: str, context: Dict) -> str:
        """
        Extraire le bloc de signature avec prise en compte du contexte (version améliorée)
        """
        
        lines = content.split('\n')
        signature_start = None
        
        # Adapter les indicateurs selon le contexte
        if context.get('email_language') == 'french':
            priority_indicators = ['cordialement', 'bien à vous', 'salutations', 'amitiés']
        else:
            priority_indicators = ['best regards', 'sincerely', 'regards', 'yours truly']
        
        # NOUVEAU: Chercher d'abord les vrais indicateurs de signature
        for i, line in enumerate(lines):
            line_lower = line.lower().strip()
            
            # Indicateurs prioritaires selon la langue (doivent être seuls sur la ligne)
            for indicator in priority_indicators:
                if line_lower == indicator or line_lower == indicator + ',':
                    signature_start = i + 1  # Commencer APRÈS la formule de politesse
                    break
            
            if signature_start is not None:
                break
        
        # Si pas d'indicateur de politesse trouvé, chercher d'autres patterns
        if signature_start is None:
            for i, line in enumerate(lines):
                line_lower = line.lower().strip()
                
                # Ligne avec tirets ou underscores (séparateur de signature)
                if re.match(r'^[-_=]{3,}$', line.strip()):
                    signature_start = i + 1
                    break
                
                # Pattern nom + titre sur des lignes consécutives
                if (i < len(lines) - 1 and 
                    re.match(r'^[A-Z][a-zA-ZÀ-ÿ\s-]+$', line) and
                    any(title in lines[i+1].lower() for title in ['avocat', 'directeur', 'manager', 'consultant'])):
                    signature_start = i
                    break
        
        # Adapter la longueur selon le type d'email
        if context.get('email_type') == 'legal':
            max_lines = 12  # Les signatures légales sont souvent plus longues
        elif context.get('email_type') == 'marketing':
            max_lines = 6   # Les emails marketing ont des signatures plus courtes
        else:
            max_lines = 10
        
        # Si pas d'indicateur trouvé, adaptation selon le contexte
        if signature_start is None:
            if context.get('formality_level') == 'formal':
                signature_start = max(0, len(lines) - max_lines)
            else:
                signature_start = max(0, len(lines) - 8)
        
        # Extraire le bloc de signature
        signature_lines = lines[signature_start:]
        
        # Filtrer selon le contexte
        filtered_lines = []
        max_line_length = 180 if context.get('email_type') == 'legal' else 150
        
        for line in signature_lines:
            line = line.strip()
            if line and len(line) < max_line_length:
                filtered_lines.append(line)
            if len(filtered_lines) >= max_lines:
                break
        
        return '\n'.join(filtered_lines)
    
    def _fallback_signature_detection(self, content: str, context: Dict) -> str:
        """
        Stratégie de fallback pour détecter la signature quand les méthodes classiques échouent
        """
        
        lines = content.split('\n')
        
        # Recherche de patterns spécifiques selon la profession
        if context.get('likely_profession') == 'legal':
            # Pour les avocats, chercher "Maître", "Avocat", etc.
            for i, line in enumerate(lines):
                if re.search(r'\b(maître|avocat|cabinet)\b', line, re.IGNORECASE):
                    return '\n'.join(lines[max(0, i-2):i+6])
        
        elif context.get('likely_profession') == 'consulting':
            # Pour les consultants, chercher "Consultant", "Directeur", etc.
            for i, line in enumerate(lines):
                if re.search(r'\b(consultant|directeur|manager)\b', line, re.IGNORECASE):
                    return '\n'.join(lines[max(0, i-1):i+5])
        
        # Stratégie basée sur la structure de l'email
        if context.get('email_type') == 'business':
            # Emails business: signature plus courte, chercher téléphone/site web
            for i, line in enumerate(lines):
                if re.search(r'(\+?\d{1,4}[.\-\s]?\d+|www\.|https?://)', line):
                    return '\n'.join(lines[max(0, i-2):i+4])
        
        # Fallback: prendre les dernières lignes non vides
        non_empty_lines = [line for line in lines if line.strip()]
        if non_empty_lines:
            return '\n'.join(non_empty_lines[-6:])
        
        return ""
    
    def _extract_contextual_info(self, signature_content: str, sender_email: str, context: Dict) -> Dict:
        """
        Extraire les informations en tenant compte du contexte
        """
        
        result = {}
        
        # Adaptation des extractions selon le contexte
        if context.get('likely_profession') == 'legal':
            result['title'] = self._extract_legal_title(signature_content, sender_email)
        elif context.get('likely_profession') == 'consulting':
            result['title'] = self._extract_consulting_title(signature_content, sender_email)
        else:
            result['title'] = self._extract_professional_title(signature_content, sender_email)
        
        # Site web et LinkedIn avec priorités selon le contexte
        result['website'] = self._extract_website(signature_content)
        result['linkedin'] = self._extract_linkedin(signature_content)
        
        # Adaptation société selon le contexte
        if context.get('domain_type') == 'legal':
            result['company'] = self._extract_legal_firm(signature_content, result.get('title'))
        else:
            result['company'] = self._extract_company(signature_content, result.get('title'))
        
        # Téléphone
        result['phone'] = self._extract_phone(signature_content)
        
        # Signature complète nettoyée
        result['signature'] = self._clean_signature(signature_content)
        
        return result
    
    def _is_person_name_only(self, line: str) -> bool:
        """
        Détecter si une ligne est uniquement un nom de personne sans titre professionnel
        """
        if not line or len(line) < 3:
            return True
        
        # Pattern pour noms de personnes simples (Prénom NOM ou Prénom NOM NOM)
        # Doit être que des lettres, espaces et traits d'union
        name_pattern = r'^[A-ZÀ-ÿ][a-zA-ZÀ-ÿ\s\-\'\.]{2,40}$'
        
        if not re.match(name_pattern, line.strip()):
            return False
        
        # Vérifier si c'est composé uniquement de mots qui ressemblent à des prénoms/noms
        words = line.strip().split()
        if len(words) < 1 or len(words) > 4:  # Noms trop courts ou trop longs
            return False
        
        # Chaque mot doit commencer par une majuscule et être principalement alphabétique
        for word in words:
            if not word[0].isupper() or len(word) < 2:
                return False
            # Exclure si le mot contient des termes professionnels
            if any(prof_term in word.lower() for prof_term in 
                   ['avocat', 'maître', 'consultant', 'directeur', 'manager', 'expert', 'conseil']):
                return False
        
        # Si tous les critères sont remplis, c'est probablement juste un nom
        return True
    
    def _extract_legal_title(self, signature: str, sender_email: str) -> Optional[str]:
        """Extraction spécialisée pour les titres juridiques"""
        
        if not signature:
            return None
        
        # Analyser ligne par ligne pour éviter les patterns multi-lignes problématiques
        for line in signature.split('\n'):
            line = line.strip()
            if not line:
                continue
            
            # Appliquer les exclusions (adaptées pour ne pas affecter les titres légaux)
            exclusion_patterns = [
                'get outlook', 'www.', 'http', 'mail de', 'objet:', 'subject:',
                'est que l', 'bonjour maître', 'cher', 'chère', 'madame', 'monsieur',
                'veuillez', 'merci', 'cordialement', 'bien à vous', 'salutations',
                'rue de', 'boulevard', 'avenue', 'place ', ', france', ' france',
                'téléphone:', 'tel:', 'mobile:', 'fax:', 'email:', 'courriel:',
                'du 25.03', 'du 24.03', 'du 26.03',
                'envoyé de mon', 'sent from my', 'envoyé depuis', 'sent from',
                'from my ipad', 'from my iphone', 'de mon ipad', 'de mon iphone'
            ]
            
            line_lower = line.lower()
            if any(pattern in line_lower for pattern in exclusion_patterns):
                continue
            
            # Patterns mobiles supplémentaires
            mobile_patterns = [
                'envoyé depuis', 'sent from', 'envoyé de mon', 'sent from my',
                'de mon ipad', 'de mon iphone', 'from my ipad', 'from my iphone',
                'outlook mobile', 'mobile outlook'
            ]
            if any(pattern in line.lower() for pattern in mobile_patterns):
                continue
            
            # Exclure les lignes qui ressemblent à des noms de personnes purs
            if self._is_person_name_only(line):
                continue
            
            # Patterns spécifiques aux professions juridiques (ligne par ligne)
            legal_patterns = [
                r'^(Maître\s+[A-Z][a-zA-ZÀ-ÿ\s-]+)$',
                r'^(Avocat[e]?\s+au\s+Barreau\s+de\s+[A-Z][a-zA-ZÀ-ÿ\s-]+)$',
                r'^(Avocat[e]?\s+[àa]\s+la\s+Cour)$',
                r'^(Avocat[e]?\s+associé[e]?)$',
                r'^(Avocat[e]?)$',
                r'^(Counsel\s+[A-Z][a-zA-ZÀ-ÿ\s-]*)$',
                r'^(Partner\s+[A-Z][a-zA-ZÀ-ÿ\s-]*)$'
            ]
            
            for pattern in legal_patterns:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    title = match.group(1).strip()
                    if 5 <= len(title) <= 80:
                        return title
        
        # Fallback sur l'extraction générique
        return self._extract_professional_title(signature, sender_email)
    
    def _extract_consulting_title(self, signature: str, sender_email: str) -> Optional[str]:
        """Extraction spécialisée pour les titres de conseil/consulting"""
        
        # Patterns spécifiques au consulting
        consulting_patterns = [
            r'(Consultant[e]?\s+Senior\s+[A-Z][a-zA-ZÀ-ÿ\s-]*)',
            r'(Directeur[rice]?\s+[A-Z][a-zA-ZÀ-ÿ\s-]*)',
            r'(Senior\s+Manager\s+[A-Z][a-zA-ZÀ-ÿ\s-]*)',
            r'(Expert[e]?\s+[A-Z][a-zA-ZÀ-ÿ\s-]*)',
            r'(Fondateur[rice]?\s+[A-Z][a-zA-ZÀ-ÿ\s-]*)'
        ]
        
        for pattern in consulting_patterns:
            match = re.search(pattern, signature, re.IGNORECASE)
            if match:
                title = match.group(1).strip()
                if len(title) > 5 and len(title) < 80:
                    return title
        
        # Fallback sur l'extraction générique
        return self._extract_professional_title(signature, sender_email)
    
    def _extract_legal_firm(self, signature: str, title: str) -> Optional[str]:
        """Extraction spécialisée pour les noms de cabinets juridiques"""
        
        # Patterns spécifiques aux cabinets d'avocats
        legal_firm_patterns = [
            r'(Cabinet\s+[A-Z][a-zA-ZÀ-ÿ\s&,-]+)',
            r'([A-Z][a-zA-ZÀ-ÿ\s&,-]+\s+Avocats?)',
            r'([A-Z][a-zA-ZÀ-ÿ\s&,-]+\s+&\s+Associés?)',
            r'(SCP\s+[A-Z][a-zA-ZÀ-ÿ\s&,-]+)',
            r'(SELARL\s+[A-Z][a-zA-ZÀ-ÿ\s&,-]+)'
        ]
        
        for line in signature.split('\n'):
            line = line.strip()
            if not line or len(line) > 80:
                continue
            
            for pattern in legal_firm_patterns:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    firm_name = match.group(1).strip()
                    if len(firm_name) > 5:
                        return firm_name
        
        # Fallback sur l'extraction générique
        return self._extract_company(signature, title)
    
    def _extract_signature_block(self, content: str) -> str:
        """
        Identifier et extraire le bloc de signature réel
        """
        
        lines = content.split('\n')
        signature_start = None
        
        # Chercher les indicateurs de début de signature
        for i, line in enumerate(lines):
            line_lower = line.lower().strip()
            
            # Indicateurs explicites de signature
            for indicator in self.signature_indicators:
                if indicator in line_lower:
                    signature_start = i
                    break
            
            # Ligne avec tirets ou underscores
            if re.match(r'^[-_=]{3,}$', line.strip()):
                signature_start = i + 1
                break
        
        # Si pas d'indicateur trouvé, prendre les dernières lignes
        if signature_start is None:
            signature_start = max(0, len(lines) - 10)
        
        # Extraire le bloc de signature
        signature_lines = lines[signature_start:]
        
        # Filtrer les lignes vides et très longues (probablement du contenu)
        filtered_lines = []
        for line in signature_lines:
            line = line.strip()
            if line and len(line) < 150:  # Ligne de signature typique < 150 chars
                filtered_lines.append(line)
            if len(filtered_lines) >= 8:  # Limiter à 8 lignes max
                break
        
        return '\n'.join(filtered_lines)
    
    def _extract_professional_title(self, signature: str, sender_email: str) -> Optional[str]:
        """
        Extraire le titre professionnel de la signature (version améliorée)
        """
        
        if not signature:
            return None
        
        # Vérifier d'abord si c'est un email marketing/spam
        spam_score = sum(1 for keyword in self.spam_keywords if keyword.lower() in signature.lower())
        if spam_score > 2:
            return None
        
        best_title = None
        best_score = 0
        
        # Analyser chaque ligne de la signature
        for line in signature.split('\n'):
            line = line.strip()
            if not line or len(line) > 100:  # Augmenter limite pour titres longs
                continue
            
            # Ignorer les lignes qui contiennent l'email
            if sender_email.split('@')[0].lower() in line.lower():
                continue
            
            # NOUVEAU: Patterns d'exclusion renforcés pour éviter le contenu d'email
            exclusion_patterns = [
                'get outlook', 'www.', 'http', 'mail de', 'objet:', 'subject:',
                'est que l', 'bonjour maître', 'cher', 'chère', 'madame', 'monsieur',
                'veuillez', 'merci', 'cordialement', 'bien à vous', 'salutations',
                'rue de', 'boulevard', 'avenue', 'place ', ', france', ' france',
                'téléphone:', 'tel:', 'mobile:', 'fax:', 'email:', 'courriel:',
                'du 25.03', 'du 24.03', 'du 26.03',  # Dates typiques d'emails
                'envoyé de mon', 'sent from my', 'envoyé depuis', 'sent from',
                'from my ipad', 'from my iphone', 'de mon ipad', 'de mon iphone'
            ]
            
            # Ignorer les lignes qui ressemblent à du contenu d'email
            line_lower = line.lower()
            if any(pattern in line_lower for pattern in exclusion_patterns):
                continue
                
            # Ignorer les lignes trop longues (probablement du contenu)
            if len(line) > 100:
                continue
                
            # Ignorer les phrases complètes (contiennent des mots de liaison)
            liaison_words = [' de ', ' du ', ' le ', ' la ', ' les ', ' un ', ' une ', ' des ', ' et ', ' ou ', ' que ', ' qui ']
            if sum(1 for word in liaison_words if word in line_lower) > 2:
                continue
            
            # Pour les patterns mobiles, ignorer complètement
            mobile_patterns = [
                'envoyé depuis', 'sent from', 'envoyé de mon', 'sent from my',
                'de mon ipad', 'de mon iphone', 'from my ipad', 'from my iphone',
                'outlook mobile', 'mobile outlook'
            ]
            if any(pattern in line.lower() for pattern in mobile_patterns):
                continue
            
            # Ignorer les noms de personnes purs (sans titre professionnel)
            if self._is_person_name_only(line):
                continue
            
            score = 0
            
            # 1. Scorer selon les titres professionnels identifiés
            for category, titles in self.professional_titles.items():
                for title in titles:
                    if re.search(rf'\b{re.escape(title)}\b', line, re.IGNORECASE):
                        score += 3 if category == 'juridique' else 2
            
            # 2. Patterns spécifiques pour titres composés
            title_patterns = [
                r'\b(directeur|directrice)\s+(général|générale|commercial|commerciale|technique|juridique)\b',
                r'\b(consultant|consultante)\s+(senior|junior)\b',
                r'\b(chef|head)\s+de\s+(projet|produit|service)\b',
                r'\b(responsable|manager)\s+(commercial|technique|juridique|marketing)\b',
                r'\bavocat[e]?\s+au\s+barreau\s+de\b',
                r'\bmaître\s+[a-zA-ZÀ-ÿ]+\b'
            ]
            
            for pattern in title_patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    score += 4
            
            # 3. Structure typique de titre (majuscule + minuscules)
            if re.match(r'^[A-Z][a-zA-ZÀ-ÿ\s\-]+$', line) and not line.isupper():
                score += 1
            
            # 4. Bonus pour certains patterns structurels
            if ' - ' in line or ' – ' in line:  # Titre avec séparateur
                score += 1
            
            if re.search(r'\b(de|du|des|at|for|in)\s+[A-Z]', line):  # "Directeur de X", "Manager at Y"
                score += 2
            
            # 5. Malus pour indicateurs de spam/problème
            if any(spam in line.lower() for spam in self.spam_keywords):
                score -= 10
            
            # Malus pour trop de caractères spéciaux
            special_chars = sum(1 for c in line if c in '.,;:!?@#$%^&*()[]{}+=<>')
            if special_chars > len(line) * 0.25:
                score -= 3
            
            # Malus pour lignes trop courtes (probablement pas un titre)
            if len(line) < 5:
                score -= 2
            
            # 6. Bonification pour longueur appropriée
            if 10 <= len(line) <= 60:
                score += 1
            
            # Garder le meilleur score
            if score > best_score and score > 1:  # Score minimum augmenté
                best_score = score
                best_title = line.strip()
        
        # Nettoyer le titre retenu
        if best_title:
            # Supprimer les caractères indésirables en début/fin
            best_title = re.sub(r'^[^\w]+|[^\w\s]+$', '', best_title).strip()
            
            # Nettoyer les doublons de mots
            words = best_title.split()
            if len(words) > 1 and words[-1].lower() == words[-2].lower():
                best_title = ' '.join(words[:-1])
            
            # Limiter la longueur
            if len(best_title) > 80:
                best_title = best_title[:80].rsplit(' ', 1)[0] + '...'
        
        return best_title if best_title and len(best_title.strip()) > 4 else None
    
    def _extract_website(self, signature: str) -> Optional[str]:
        """
        Extraire le site web principal (pas les liens de tracking/marketing)
        """
        
        if not signature:
            return None
        
        # Pattern pour URLs valides
        url_pattern = r'https?://(?:www\.)?([a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+)(?:/[^\s]*)?'
        
        urls = re.findall(url_pattern, signature)
        
        # Filtrer les URLs indésirables
        excluded_domains = [
            'unsubscribe', 'tracking', 'pixel', 'analytics', 'marketing', 'substack',
            'mailchimp', 'constantcontact', 'newsletter', 'campaign', 'email',
            'linkedin.com', 'facebook.com', 'twitter.com', 'instagram.com',
            'youtube.com', 'google.com', 'microsoft.com', 'outlook.com'
        ]
        
        for url in urls:
            domain = url.lower()
            if not any(excluded in domain for excluded in excluded_domains):
                return f"https://{url}"
        
        return None
    
    def _extract_linkedin(self, signature: str) -> Optional[str]:
        """
        Extraire le profil LinkedIn spécifiquement
        """
        
        # Pattern pour URLs LinkedIn
        linkedin_pattern = r'https?://(?:www\.)?linkedin\.com/in/([a-zA-Z0-9-]+)'
        
        match = re.search(linkedin_pattern, signature)
        if match:
            return f"https://linkedin.com/in/{match.group(1)}"
        
        return None
    
    def _extract_company(self, signature: str, title: str) -> Optional[str]:
        """
        Extraire le nom de la société/cabinet
        """
        
        if not signature:
            return None
        
        # Chercher des patterns de société
        company_patterns = [
            r'(?:cabinet|société|sa|sarl|sas|eurl|snc)\s+([a-zA-ZÀ-ÿ\s&-]+)',
            r'([A-Z][a-zA-ZÀ-ÿ\s&-]+)\s+(?:cabinet|société|sa|sarl|sas|eurl)',
            r'^([A-Z][a-zA-ZÀ-ÿ\s&-]{3,40})$'  # Ligne en majuscules (nom société typique)
        ]
        
        for line in signature.split('\n'):
            line = line.strip()
            if not line or len(line) > 50:
                continue
            
            for pattern in company_patterns:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    company_name = match.group(1).strip()
                    if (len(company_name) > 3 and 
                        not any(spam in company_name.lower() for spam in self.spam_keywords)):
                        return company_name
        
        return None
    
    def _extract_phone(self, signature: str) -> Optional[str]:
        """
        Extraire le numéro de téléphone
        """
        
        # Patterns téléphone français et internationaux
        phone_patterns = [
            r'(?:\+33|0)[1-9](?:[.\-\s]?\d{2}){4}',  # Format français
            r'\+\d{1,4}[.\-\s]?\d{1,4}[.\-\s]?\d{1,4}[.\-\s]?\d{1,9}',  # International
            r'\d{2}[.\-\s]?\d{2}[.\-\s]?\d{2}[.\-\s]?\d{2}[.\-\s]?\d{2}'  # Format groupé
        ]
        
        for pattern in phone_patterns:
            match = re.search(pattern, signature)
            if match:
                phone = match.group(0)
                # Nettoyer le format
                phone = re.sub(r'[^\d+]', '', phone)
                if len(phone) >= 8:  # Minimum 8 chiffres
                    return match.group(0)  # Retourner format original
        
        return None
    
    def _clean_signature(self, signature: str) -> Optional[str]:
        """
        Nettoyer et formatter la signature complète
        """
        
        if not signature:
            return None
        
        lines = []
        for line in signature.split('\n'):
            line = line.strip()
            if (line and 
                len(line) < 100 and 
                not any(spam in line.lower() for spam in self.spam_keywords)):
                lines.append(line)
            if len(lines) >= 4:  # Limiter à 4 lignes
                break
        
        if lines:
            return ' | '.join(lines)
        
        return None
    
    def analyze_extraction_quality(self, result: Dict) -> Dict[str, float]:
        """
        Analyser la qualité de l'extraction pour debugging
        
        Returns:
            Dict avec scores de qualité pour chaque champ
        """
        
        quality_scores = {}
        
        # Score titre
        title = result.get('title', '')
        if title:
            title_score = 0.0
            # Bonus pour mots-clés professionnels
            for category, titles in self.professional_titles.items():
                for prof_title in titles:
                    if prof_title.lower() in title.lower():
                        title_score += 0.3
            
            # Malus pour spam
            spam_count = sum(1 for spam in self.spam_keywords if spam.lower() in title.lower())
            title_score -= spam_count * 0.2
            
            # Bonus structure
            if re.match(r'^[A-Z][a-zA-ZÀ-ÿ\s]+$', title):
                title_score += 0.2
            
            quality_scores['title'] = max(0.0, min(1.0, title_score))
        
        # Score website
        website = result.get('website', '')
        if website:
            quality_scores['website'] = 0.8 if website.startswith('https://') else 0.5
        
        # Score LinkedIn
        linkedin = result.get('linkedin', '')
        if linkedin:
            quality_scores['linkedin'] = 0.9 if 'linkedin.com/in/' in linkedin else 0.3
        
        return quality_scores