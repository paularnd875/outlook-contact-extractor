from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
import httpx
import logging
import asyncio
from datetime import datetime
from bs4 import BeautifulSoup
import re

from app.database import get_db, Contact

ai_router = APIRouter()
logger = logging.getLogger(__name__)

# Configuration Hugging Face (gratuit)
HUGGING_FACE_API_URL = "https://api-inference.huggingface.co/models/facebook/bart-large-mnli"
HUGGING_FACE_TOKEN = None  # Will be set via environment variable

class ProfileType(BaseModel):
    id: str
    name: str
    description: str
    keywords: List[str]
    color: str = "primary"

class ContactClassification(BaseModel):
    contact_id: int
    profile_type: str
    confidence: float
    reasoning: str

class AIClassificationRequest(BaseModel):
    session_id: str
    profile_types: List[ProfileType]
    batch_size: Optional[int] = 10

class LinkedInScrapeRequest(BaseModel):
    linkedin_url: str

# Profils de classification (définis d'après les catégories de la cliente avocate "Andréa")
# Ces descriptions servent de consignes au classifieur (mots-clés aujourd'hui, LLM Ollama ensuite).
DEFAULT_PROFILES = [
    ProfileType(
        id="avocat",
        name="Avocat",
        description="Un autre avocat : confrère ou consœur d'un autre cabinet ou barreau (mention 'Maître', 'avocat au barreau de', nom de cabinet d'avocats).",
        keywords=["maître", "avocat", "avocate", "barreau", "cabinet", "confrère", "consœur", "consoeur", "au barreau de"],
        color="info"
    ),
    ProfileType(
        id="partenaire",
        name="Partenaire",
        description="Partenaire ou prescripteur professionnel qui n'est PAS avocat : notaire, expert-comptable, huissier/commissaire de justice, apporteur d'affaires, prestataire, partenaire qui recommande ou collabore.",
        keywords=["notaire", "expert-comptable", "comptable", "huissier", "commissaire de justice", "partenaire", "partenariat", "apporteur", "recommandation", "prescripteur", "collaboration", "prestataire"],
        color="warning"
    ),
    ProfileType(
        id="prospect",
        name="Prospect",
        description="Contact potentiel, pas encore client : première prise de contact, demande de renseignements ou de rendez-vous, devis envoyé mais non signé, aucun dossier ouvert.",
        keywords=["renseignement", "demande d'information", "première consultation", "devis", "premier rendez-vous", "prise de contact", "tarif", "honoraires", "vous contacte"],
        color="primary"
    ),
    ProfileType(
        id="client_actif",
        name="Client actif",
        description="Client avec un dossier en cours et des échanges récents : convention d'honoraires signée, procédure ou dossier en cours, factures/règlements, échanges des derniers mois.",
        keywords=["dossier", "procédure", "convention d'honoraires", "audience", "facture", "règlement", "en cours", "votre affaire", "votre dossier", "conclusions"],
        color="success"
    ),
    ProfileType(
        id="client_inactif",
        name="Client inactif",
        description="Ancien client : dossier clôturé/archivé, affaire terminée, plus aucun échange récent (relation passée sans activité depuis longtemps).",
        keywords=["dossier clôturé", "dossier classé", "affaire terminée", "archivé", "clôture", "solde de tout compte", "merci pour votre confiance"],
        color="secondary"
    ),
    ProfileType(
        id="autre",
        name="Autre",
        description="Tout le reste : newsletters, marketing, administratif, expéditeurs automatiques, contacts personnels, ou cas indéterminé ne rentrant dans aucune autre catégorie.",
        keywords=["newsletter", "marketing", "publicité", "promo", "unsubscribe", "se désinscrire", "spam", "no-reply", "ne pas répondre", "automatique"],
        color="dark"
    )
]

@ai_router.get("/profile-types")
async def get_profile_types():
    """Récupérer les types de profils disponibles"""
    return {"profiles": [profile.dict() for profile in DEFAULT_PROFILES]}

@ai_router.post("/classify-contacts")
async def classify_contacts(
    request: AIClassificationRequest,
    db: AsyncSession = Depends(get_db)
):
    """Classifier les contacts avec l'IA"""
    
    try:
        # Récupérer les contacts de la session
        result = await db.execute(
            select(Contact).where(Contact.session_id == request.session_id)
        )
        contacts = result.scalars().all()
        
        if not contacts:
            raise HTTPException(status_code=404, detail="Aucun contact trouvé pour cette session")
        
        classifications = []
        processed_count = 0
        
        # Traitement par batch pour éviter la surcharge
        batch_size = request.batch_size or 10
        
        for i in range(0, len(contacts), batch_size):
            batch = contacts[i:i + batch_size]
            
            for contact in batch:
                try:
                    classification = await classify_single_contact(contact, request.profile_types)
                    classifications.append(classification)
                    
                    # NOUVEAU: Sauvegarder la classification dans la base de données
                    contact.classification = classification.profile_type
                    contact.justification_classification = classification.reasoning
                    contact.confiance_classification = int(classification.confidence * 100)
                    await db.commit()
                    
                    processed_count += 1
                except Exception as e:
                    logger.error(f"Erreur classification contact {contact.id}: {e}")
                    # Classification par défaut en cas d'erreur
                    default_classification = ContactClassification(
                        contact_id=contact.id,
                        profile_type="unknown",
                        confidence=0.1,
                        reasoning="Erreur lors de la classification automatique"
                    )
                    classifications.append(default_classification)
                    
                    # Sauvegarder la classification par défaut
                    contact.classification = "autre"
                    contact.justification_classification = "Erreur lors de la classification automatique"
                    contact.confiance_classification = 10
                    await db.commit()
            
            # Petite pause entre les batches pour éviter les limites de taux (réduite pour le MVP)
            await asyncio.sleep(0.1)
        
        return {
            "total_contacts": len(contacts),
            "processed_contacts": processed_count,
            "classifications": [c.dict() for c in classifications]
        }
        
    except Exception as e:
        logger.error(f"Erreur globale classification: {e}")
        raise HTTPException(status_code=500, detail=f"Erreur lors de la classification: {str(e)}")

async def classify_single_contact(contact: Contact, profile_types: List[ProfileType]) -> ContactClassification:
    """Classifier un seul contact avec l'IA Hugging Face"""
    
    # Construire le texte à analyser
    contact_text = build_contact_text(contact)
    
    # Si pas de token Hugging Face, utiliser la classification par mots-clés
    if not HUGGING_FACE_TOKEN:
        return classify_by_keywords(contact, contact_text, profile_types)
    
    try:
        # Classification avec Hugging Face BART
        classification = await classify_with_huggingface(contact_text, profile_types)
        return ContactClassification(
            contact_id=contact.id,
            profile_type=classification["profile"],
            confidence=classification["confidence"],
            reasoning=classification["reasoning"]
        )
    except Exception as e:
        logger.error(f"Erreur Hugging Face pour contact {contact.id}: {e}")
        # Fallback sur classification par mots-clés
        return classify_by_keywords(contact, contact_text, profile_types)

def build_contact_text(contact: Contact) -> str:
    """Construire le texte représentatif du contact pour l'analyse"""
    parts = []
    
    if contact.nom_complet:
        parts.append(f"Nom: {contact.nom_complet}")
    elif contact.nom or contact.prenom:
        parts.append(f"Nom: {(contact.prenom or '')} {(contact.nom or '')}")
    
    if contact.email:
        parts.append(f"Email: {contact.email}")
    
    if contact.intitule:
        parts.append(f"Titre: {contact.intitule}")
    
    if contact.site_web:
        parts.append(f"Site web: {contact.site_web}")
    
    if contact.signature_complete:
        # Limiter la signature pour éviter trop de texte
        signature = contact.signature_complete[:500] + "..." if len(contact.signature_complete) > 500 else contact.signature_complete
        parts.append(f"Signature: {signature}")
    
    return " | ".join(parts)

async def classify_with_huggingface(contact_text: str, profile_types: List[ProfileType]) -> Dict[str, Any]:
    """Classification avec l'API Hugging Face"""
    
    # Préparer les hypothèses pour BART-MNLI
    candidate_labels = [f"Ce contact est un {profile.name.lower()}: {profile.description}" for profile in profile_types]
    
    headers = {
        "Authorization": f"Bearer {HUGGING_FACE_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "inputs": contact_text,
        "parameters": {
            "candidate_labels": candidate_labels
        }
    }
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(HUGGING_FACE_API_URL, json=payload, headers=headers)
        
        if response.status_code != 200:
            raise Exception(f"Erreur API Hugging Face: {response.status_code}")
        
        result = response.json()
        
        # Extraire le meilleur résultat
        best_label = result["labels"][0]
        best_score = result["scores"][0]
        
        # Retrouver le type de profil correspondant
        profile_type = "unknown"
        for i, profile in enumerate(profile_types):
            if candidate_labels[i] == best_label:
                profile_type = profile.id
                break
        
        return {
            "profile": profile_type,
            "confidence": best_score,
            "reasoning": f"Classification IA basée sur l'analyse du contenu (score: {best_score:.2f})"
        }

def classify_by_keywords(contact: Contact, contact_text: str, profile_types: List[ProfileType]) -> ContactClassification:
    """Classification basique par mots-clés (fallback)"""
    
    contact_text_lower = contact_text.lower()
    best_profile = "unknown"
    best_score = 0.0
    matching_keywords = []
    
    for profile in profile_types:
        score = 0
        matched = []
        
        for keyword in profile.keywords:
            if keyword.lower() in contact_text_lower:
                score += 1
                matched.append(keyword)
        
        # Normaliser le score par le nombre de mots-clés
        if len(profile.keywords) > 0:
            normalized_score = score / len(profile.keywords)
            
            if normalized_score > best_score:
                best_score = normalized_score
                best_profile = profile.id
                matching_keywords = matched
    
    # Si aucun mot-clé trouvé, essayer de déduire du domaine email
    if best_score == 0 and contact.email:
        domain = contact.email.split('@')[-1].lower() if '@' in contact.email else ""
        
        # Heuristiques simples basées sur le domaine
        if any(term in domain for term in ["gmail", "hotmail", "yahoo", "outlook", "icloud"]):
            best_profile = "personnel"
            best_score = 0.3
            matching_keywords = ["domaine personnel"]
        elif any(term in domain for term in ["company", "corp", "inc", "ltd", "sarl", "sas"]):
            best_profile = "client"
            best_score = 0.4
            matching_keywords = ["domaine professionnel"]
    
    reasoning = f"Classification par mots-clés. Mots trouvés: {', '.join(matching_keywords)}" if matching_keywords else "Aucun indicateur trouvé, classification par défaut"
    
    return ContactClassification(
        contact_id=contact.id,
        profile_type=best_profile,
        confidence=max(best_score, 0.1),  # Confidence minimum
        reasoning=reasoning
    )

@ai_router.get("/classification-stats/{session_id}")
async def get_classification_stats(session_id: str, db: AsyncSession = Depends(get_db)):
    """Obtenir les statistiques de classification pour une session"""
    
    # Note: Cette fonction nécessiterait une table supplémentaire pour stocker les classifications
    # Pour le MVP, retourner des statistiques simulées
    
    total_result = await db.execute(
        select(func.count(Contact.id)).where(Contact.session_id == session_id)
    )
    total_contacts = total_result.scalar()
    
    if total_contacts == 0:
        raise HTTPException(status_code=404, detail="Aucun contact trouvé pour cette session")
    
    # Statistiques simulées pour le MVP
    stats = {
        "total_contacts": total_contacts,
        "classified_contacts": 0,  # Sera mis à jour quand on aura une vraie DB de classifications
        "profile_distribution": {
            "client": 0,
            "prescripteur": 0,
            "partenaire": 0,
            "personnel": 0,
            "unknown": total_contacts
        },
        "average_confidence": 0.0
    }
    
    return stats

@ai_router.post("/update-contact-profile")
async def update_contact_profile(
    contact_id: int,
    profile_type: str,
    db: AsyncSession = Depends(get_db)
):
    """Mettre à jour manuellement le profil d'un contact"""
    
    # Pour le MVP, on peut stocker dans le champ type_contact existant
    result = await db.execute(
        select(Contact).where(Contact.id == contact_id)
    )
    contact = result.scalar_one_or_none()
    
    if not contact:
        raise HTTPException(status_code=404, detail="Contact non trouvé")
    
    # Valider le type de profil
    valid_profiles = [p.id for p in DEFAULT_PROFILES] + ["unknown"]
    if profile_type not in valid_profiles:
        raise HTTPException(status_code=400, detail="Type de profil invalide")
    
    contact.type_contact = profile_type
    await db.commit()
    
    return {"message": "Profil mis à jour", "contact_id": contact_id, "profile_type": profile_type}

@ai_router.post("/scrape-linkedin")
async def scrape_linkedin_profile(request: LinkedInScrapeRequest):
    """Scraper un profil LinkedIn pour extraire les informations professionnelles"""
    
    try:
        # Headers pour simuler un navigateur
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Tenter de récupérer la page LinkedIn
            response = await client.get(request.linkedin_url, headers=headers)
            
            if response.status_code != 200:
                logger.warning(f"Erreur HTTP {response.status_code} lors du scraping LinkedIn")
                return await fallback_linkedin_analysis(request.linkedin_url)
            
            # Parser le HTML avec BeautifulSoup
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Extraire les informations disponibles publiquement
            profile_info = await extract_linkedin_info(soup, request.linkedin_url)
            
            if not profile_info["success"]:
                return await fallback_linkedin_analysis(request.linkedin_url)
                
            return {
                "success": True,
                "profile_description": profile_info["description"],
                "extracted_info": profile_info["details"]
            }
            
    except Exception as e:
        logger.error(f"Erreur lors du scraping LinkedIn: {e}")
        # Fallback vers une analyse basique de l'URL
        return await fallback_linkedin_analysis(request.linkedin_url)

async def extract_linkedin_info(soup: BeautifulSoup, linkedin_url: str) -> Dict[str, Any]:
    """Extraire les informations d'un profil LinkedIn depuis le HTML"""
    
    try:
        # Extraire le nom
        name_selectors = [
            'h1.text-heading-xlarge',
            'h1[data-anonymize="person-name"]',
            '.pv-text-details__main-heading h1',
            'h1.break-words'
        ]
        name = None
        for selector in name_selectors:
            name_element = soup.select_one(selector)
            if name_element:
                name = name_element.get_text().strip()
                break
        
        # Extraire le titre/poste
        title_selectors = [
            '.text-body-medium.break-words',
            '.pv-text-details__sub-heading',
            'div.text-body-medium'
        ]
        title = None
        for selector in title_selectors:
            title_element = soup.select_one(selector)
            if title_element:
                title = title_element.get_text().strip()
                break
        
        # Extraire l'expérience/description
        experience_selectors = [
            'div.pv-shared-text-with-see-more',
            'div[data-generated-suggestion-target]',
            '.pv-about__summary-text'
        ]
        experience = None
        for selector in experience_selectors:
            exp_element = soup.select_one(selector)
            if exp_element:
                experience = exp_element.get_text().strip()
                break
        
        # Analyser l'URL pour déterminer le domaine d'activité
        url_analysis = analyze_linkedin_url(linkedin_url)
        
        if name or title or experience:
            # Construire une description basée sur les informations extraites
            description_parts = []
            
            if name:
                description_parts.append(f"Professionnel identifié : {name}")
            
            if title:
                description_parts.append(f"Poste/Fonction : {title}")
                # Analyser le titre pour déterminer le domaine
                domain = analyze_professional_title(title)
                if domain:
                    description_parts.append(f"Domaine d'activité : {domain}")
            
            if experience:
                description_parts.append(f"Description professionnelle : {experience[:500]}...")
            
            # Ajouter l'analyse de l'URL
            if url_analysis:
                description_parts.append(f"Spécialisation détectée : {url_analysis}")
            
            description = "\n\n".join(description_parts)
            
            return {
                "success": True,
                "description": description,
                "details": {
                    "name": name,
                    "title": title,
                    "experience": experience[:1000] if experience else None,
                    "url_analysis": url_analysis
                }
            }
    
    except Exception as e:
        logger.error(f"Erreur lors de l'extraction LinkedIn: {e}")
    
    return {"success": False}

def analyze_linkedin_url(url: str) -> Optional[str]:
    """Analyser l'URL LinkedIn pour détecter le domaine d'activité"""
    url_lower = url.lower()
    
    specializations = {
        'strategie-patrimoniale': 'Conseil en stratégie patrimoniale et gestion de patrimoine',
        'avocat': 'Droit et conseil juridique',
        'notaire': 'Notariat et transactions immobilières',
        'expert-comptable': 'Expertise comptable et conseil fiscal',
        'consultant': 'Conseil en entreprise',
        'finance': 'Services financiers',
        'immobilier': 'Immobilier et transactions',
        'marketing': 'Marketing et communication',
        'rh': 'Ressources humaines',
        'it': 'Technologies de l\'information'
    }
    
    for keyword, specialization in specializations.items():
        if keyword in url_lower:
            return specialization
    
    return None

def analyze_professional_title(title: str) -> Optional[str]:
    """Analyser un titre professionnel pour déterminer le domaine"""
    title_lower = title.lower()
    
    domains = {
        'avocat': 'Droit et conseil juridique',
        'notaire': 'Notariat et transactions',
        'expert-comptable': 'Expertise comptable',
        'consultant': 'Conseil en entreprise',
        'directeur': 'Direction et management',
        'manager': 'Management',
        'développeur': 'Développement informatique',
        'commercial': 'Commerce et vente',
        'marketing': 'Marketing et communication',
        'finance': 'Finance et comptabilité',
        'rh': 'Ressources humaines',
        'patrimoine': 'Gestion de patrimoine'
    }
    
    for keyword, domain in domains.items():
        if keyword in title_lower:
            return domain
    
    return None

async def fallback_linkedin_analysis(linkedin_url: str) -> Dict[str, Any]:
    """Analyse de fallback basée sur l'URL quand le scraping échoue"""
    
    url_analysis = analyze_linkedin_url(linkedin_url)
    
    # Extraction basique du nom d'utilisateur depuis l'URL
    username = None
    if '/in/' in linkedin_url:
        username_part = linkedin_url.split('/in/')[-1].split('/')[0].split('?')[0]
        # Nettoyer et formater le nom d'utilisateur
        username = username_part.replace('-', ' ').title()
    
    description = f"Profil LinkedIn analysé : {linkedin_url}\n\n"
    
    if username:
        description += f"Identifiant professionnel : {username}\n\n"
    
    if url_analysis:
        description += f"Domaine d'activité détecté : {url_analysis}\n\n"
    
    description += """Informations limitées disponibles publiquement. Pour une analyse complète, 
veuillez vous assurer que le profil LinkedIn est public et accessible, ou 
renseignez manuellement votre contexte professionnel."""
    
    return {
        "success": True,
        "profile_description": description,
        "extracted_info": {
            "username": username,
            "url_analysis": url_analysis,
            "method": "fallback_url_analysis"
        }
    }