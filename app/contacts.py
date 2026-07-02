from fastapi import APIRouter, HTTPException, Depends, Query, BackgroundTasks, Request
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from typing import Optional, List
import io
import csv
from datetime import datetime, timedelta
import uuid

from app.database import get_db, Contact, ExtractionSession, get_or_create_contact
from app.graph_extractor import GraphExtractor
from app.contact_processor import ContactProcessor
from app.normalizer import normalize_name

contacts_router = APIRouter()

@contacts_router.post("/extract")
async def start_extraction(
    request: Request,
    background_tasks: BackgroundTasks,
    period_months: float = Query(default=1, ge=0.25, le=120, description="Période d'extraction en mois"),
    db: AsyncSession = Depends(get_db)
):
    """Démarrer l'extraction des contacts"""

    # Récupérer l'utilisateur depuis les tokens stockés
    from app.auth import user_tokens

    # Utiliser le compte réellement connecté dans CETTE session navigateur
    # (et non "le premier connecté", qui pourrait viser la mauvaise boîte mail)
    user_id = request.session.get('user_id')
    if not user_id or user_id not in user_tokens:
        # Repli: s'il n'y a qu'un seul compte connecté, l'utiliser
        if len(user_tokens) == 1:
            user_id = next(iter(user_tokens))
        else:
            raise HTTPException(
                status_code=401,
                detail="Aucun compte connecté pour cette session. Reconnectez-vous avec la boîte mail à analyser."
            )
    
    try:
        # Créer une session d'extraction
        session_id = str(uuid.uuid4())
        extraction_session = ExtractionSession(
            id=session_id,
            user_id=user_id,
            email_address=f"user_{user_id}",  # Sera mis à jour par l'extracteur
            periode_debut=datetime.utcnow() - timedelta(days=period_months * 30),
            periode_fin=datetime.utcnow()
        )
        
        db.add(extraction_session)
        await db.commit()
        
        # Lancer l'extraction en arrière-plan
        background_tasks.add_task(
            extract_contacts_task,
            session_id,
            user_id,
            period_months
        )
        
        return {
            "session_id": session_id,
            "message": "Extraction démarrée en arrière-plan",
            "status": "in_progress"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors du démarrage de l'extraction: {str(e)}")

@contacts_router.get("/extraction/{session_id}/status")
async def get_extraction_status(session_id: str, db: AsyncSession = Depends(get_db)):
    """Obtenir le statut d'une extraction"""
    
    result = await db.execute(
        select(ExtractionSession).where(ExtractionSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(status_code=404, detail="Session d'extraction non trouvée")
    
    return session.to_dict()

@contacts_router.get("/sessions")
async def get_user_sessions(db: AsyncSession = Depends(get_db)):
    """Récupérer les sessions d'extraction de l'utilisateur connecté"""
    
    # Récupérer l'utilisateur depuis les tokens stockés
    from app.auth import user_tokens
    
    if not user_tokens:
        raise HTTPException(status_code=401, detail="Aucun utilisateur connecté")
    
    user_id = list(user_tokens.keys())[0]
    
    result = await db.execute(
        select(ExtractionSession)
        .where(ExtractionSession.user_id == user_id)
        .order_by(ExtractionSession.date_debut.desc())
        .limit(1)
    )
    session = result.scalar_one_or_none()
    
    if session:
        return session.to_dict()
    else:
        return None

@contacts_router.get("/contacts")
async def get_contacts(
    session_id: str = Query(..., description="ID de la session d'extraction"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
    search: Optional[str] = Query(default=None, description="Recherche dans nom/email"),
    filter_type: Optional[str] = Query(default=None, description="Filtrer par type de contact"),
    filter_classification: Optional[str] = Query(default=None, description="Filtrer par classification IA"),
    filter_validated: Optional[bool] = Query(default=None, description="Filtrer par validation"),
    sort_by: Optional[str] = Query(default="date_dernier_contact", description="Colonne de tri"),
    sort_order: Optional[str] = Query(default="desc", description="Ordre de tri"),
    db: AsyncSession = Depends(get_db)
):
    """Récupérer la liste des contacts avec pagination et filtres"""
    
    try:
        # Construction de la requête de base
        query = select(Contact).where(Contact.session_id == session_id)
        
        # Filtres
        if search:
            search_term = f"%{search}%"
            query = query.where(
                Contact.nom_complet.ilike(search_term) |
                Contact.email.ilike(search_term) |
                Contact.nom.ilike(search_term) |
                Contact.prenom.ilike(search_term)
            )
        
        if filter_type:
            query = query.where(Contact.type_contact == filter_type)
            
        if filter_classification:
            query = query.where(Contact.type_contact == filter_classification)
        
        if filter_validated is not None:
            query = query.where(Contact.valide == filter_validated)
        
        # Tri
        sort_column = getattr(Contact, sort_by, Contact.date_dernier_contact)
        if sort_order.lower() == "desc":
            query = query.order_by(sort_column.desc())
        else:
            query = query.order_by(sort_column.asc())
        
        # Compter le total
        count_query = select(func.count(Contact.id)).where(Contact.session_id == session_id)
        if search:
            search_term = f"%{search}%"
            count_query = count_query.where(
                Contact.nom_complet.ilike(search_term) |
                Contact.email.ilike(search_term) |
                Contact.nom.ilike(search_term) |
                Contact.prenom.ilike(search_term)
            )
        
        total_result = await db.execute(count_query)
        total = total_result.scalar()
        
        # Pagination
        offset = (page - 1) * limit
        query = query.offset(offset).limit(limit)
        
        # Exécuter la requête
        result = await db.execute(query)
        contacts = result.scalars().all()
        
        return {
            "contacts": [contact.to_dict() for contact in contacts],
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "pages": (total + limit - 1) // limit
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de la récupération des contacts: {str(e)}")

@contacts_router.put("/contacts/{contact_id}/validate")
async def update_contact_validation(
    contact_id: int,
    validated: bool,
    db: AsyncSession = Depends(get_db)
):
    """Mettre à jour le statut de validation d'un contact"""
    
    result = await db.execute(
        select(Contact).where(Contact.id == contact_id)
    )
    contact = result.scalar_one_or_none()
    
    if not contact:
        raise HTTPException(status_code=404, detail="Contact non trouvé")
    
    contact.valide = validated
    await db.commit()
    
    return {"message": "Statut de validation mis à jour", "validated": validated}

@contacts_router.get("/contacts/export")
async def export_contacts(
    session_id: str = Query(..., description="ID de la session d'extraction"),
    validated_only: bool = Query(default=True, description="Exporter uniquement les contacts validés"),
    db: AsyncSession = Depends(get_db)
):
    """Exporter les contacts au format CSV"""
    
    try:
        # Construction de la requête
        query = select(Contact).where(Contact.session_id == session_id)
        
        if validated_only:
            query = query.where(Contact.valide == True)
        
        query = query.order_by(Contact.nom_complet.asc())
        
        # Récupérer les contacts
        result = await db.execute(query)
        contacts = result.scalars().all()
        
        # Créer le fichier CSV en mémoire
        output = io.StringIO()
        writer = csv.writer(output)
        
        # En-têtes
        headers = [
            'Nom', 'Prénom', 'Nom_Complet', 'Adresse_Mail', 
            'Intitulé', 'Site_Web', 'LinkedIn', 'Type_Contact',
            'Classification_IA', 'Justification_Classification', 'Confiance_Classification',
            'Nombre_Emails', 'Date_Premier_Contact', 'Date_Dernier_Contact'
        ]
        writer.writerow(headers)
        
        # Données
        for contact in contacts:
            # Combiner le nom avec l'email pour la colonne "Nom"
            nom_avec_email = f"{contact.nom or ''} ({contact.email})" if contact.nom else contact.email
            
            # Obtenir le nom de la classification IA pour l'export
            classification_labels = {
                'client': 'Client',
                'prospect': 'Prospect',
                'avocat': 'Avocat',
                'autre': 'Autre',
                'unknown': 'Non classé'
            }
            classification_ia = classification_labels.get(contact.classification, contact.classification or 'Non classé')
            
            row = [
                nom_avec_email,  # Nom avec email inclus
                contact.prenom or '',
                contact.nom_complet or '',
                contact.email,
                contact.intitule or '',
                contact.site_web or '',
                contact.linkedin_url or '',
                contact.type_contact or '',
                classification_ia,  # Classification IA en français
                contact.justification_classification or '',  # Justification de l'IA
                f"{contact.confiance_classification}%" if contact.confiance_classification else '',  # Score confiance
                contact.nombre_emails,
                contact.date_premier_contact.strftime('%Y-%m-%d %H:%M:%S') if contact.date_premier_contact else '',
                contact.date_dernier_contact.strftime('%Y-%m-%d %H:%M:%S') if contact.date_dernier_contact else ''
            ]
            writer.writerow(row)
        
        # Préparer la réponse
        output.seek(0)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"contacts_outlook_{timestamp}.csv"
        
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode('utf-8-sig')),  # UTF-8 avec BOM pour Excel
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de l'export: {str(e)}")

@contacts_router.get("/stats")
async def get_extraction_stats(
    session_id: str = Query(..., description="ID de la session d'extraction"),
    db: AsyncSession = Depends(get_db)
):
    """Obtenir les statistiques d'une extraction"""
    
    try:
        # Statistiques générales
        total_result = await db.execute(
            select(func.count(Contact.id)).where(Contact.session_id == session_id)
        )
        total_contacts = total_result.scalar()
        
        # Contacts validés
        validated_result = await db.execute(
            select(func.count(Contact.id)).where(
                and_(Contact.session_id == session_id, Contact.valide == True)
            )
        )
        validated_contacts = validated_result.scalar()
        
        # Répartition par type
        type_stats_result = await db.execute(
            select(Contact.type_contact, func.count(Contact.id))
            .where(Contact.session_id == session_id)
            .group_by(Contact.type_contact)
        )
        type_stats = dict(type_stats_result.fetchall())
        
        return {
            "total_contacts": total_contacts,
            "validated_contacts": validated_contacts,
            "invalidated_contacts": total_contacts - validated_contacts,
            "type_distribution": type_stats,
            "validation_rate": (validated_contacts / total_contacts * 100) if total_contacts > 0 else 0
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de la récupération des statistiques: {str(e)}")

@contacts_router.post("/contacts/bulk-validate")
async def bulk_validate_contacts(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Validation en lot des contacts selon des critères"""
    
    try:
        body = await request.json()
        session_id = body.get('session_id')
        validated = body.get('validated', True)
        
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id requis")
        
        # Construction de la requête de base
        query = select(Contact).where(Contact.session_id == session_id)
        
        # Appliquer les filtres optionnels
        if 'filter_type' in body and body['filter_type']:
            query = query.where(Contact.type_contact == body['filter_type'])
            
        if 'filter_classification' in body and body['filter_classification']:
            query = query.where(Contact.type_contact == body['filter_classification'])
            
        if 'filter_validated' in body and body['filter_validated'] is not None:
            if body['filter_validated'] == 'true':
                query = query.where(Contact.valide == True)
            elif body['filter_validated'] == 'false':
                query = query.where(Contact.valide == False)
                
        if 'search' in body and body['search']:
            search_term = f"%{body['search']}%"
            query = query.where(
                or_(
                    Contact.email.like(search_term),
                    Contact.nom.like(search_term),
                    Contact.prenom.like(search_term),
                    Contact.nom_complet.like(search_term),
                    Contact.intitule.like(search_term)
                )
            )
        
        # Récupérer les contacts correspondants
        result = await db.execute(query)
        contacts = result.scalars().all()
        
        # Mettre à jour le statut de validation
        updated_count = 0
        for contact in contacts:
            contact.valide = validated
            updated_count += 1
        
        await db.commit()
        
        return {
            "message": f"{updated_count} contacts mis à jour",
            "updated_count": updated_count,
            "validated": validated
        }
        
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Erreur lors de la validation en lot: {str(e)}")

async def extract_contacts_task(session_id: str, user_id: str, period_months: float):
    """Tâche d'extraction des contacts en arrière-plan"""
    
    from app.database import SessionLocal
    import logging
    
    logger = logging.getLogger(__name__)
    
    async with SessionLocal() as db:
        extraction_session = None
        try:
            # Récupérer la session d'extraction
            result = await db.execute(
                select(ExtractionSession).where(ExtractionSession.id == session_id)
            )
            extraction_session = result.scalar_one()
            
            # Récupérer le token d'accès de l'utilisateur
            from app.auth import get_user_token
            access_token = get_user_token(user_id)
            
            if not access_token:
                raise Exception("Token d'accès introuvable ou expiré")
            
            # Utiliser GraphExtractor pour l'extraction réelle
            from app.graph_extractor import GraphExtractor
            from app.contact_processor import ContactProcessor
            
            total_contacts = 0
            total_emails = 0
            processed_emails = 0
            
            logger.info(f"Démarrage extraction pour session {session_id}")
            
            # Mode léger (en-têtes seuls) quand la classification IA est désactivée :
            # extraction "carnet d'adresses" rapide et robuste, sans télécharger les corps.
            from app.enriched_classification import CLASSIFICATION_ENABLED
            async with GraphExtractor(access_token, light=not CLASSIFICATION_ENABLED) as extractor:
                processor = ContactProcessor(db, session_id)
                
                # Extraire les contacts
                async for contact_data in extractor.extract_contacts(period_months):
                    processed_emails += 1
                    
                    try:
                        contact = await processor.process_contact(contact_data)
                        if contact:
                            total_contacts += 1
                            
                    except Exception as contact_error:
                        logger.error(f"Erreur traitement contact {contact_data.get('email', 'unknown')}: {contact_error}")
                        # Rollback de cette transaction et continuer
                        await db.rollback()
                        continue
                    
                    # Mettre à jour le progrès toutes les 10 emails
                    if processed_emails % 10 == 0:
                        try:
                            extraction_session.total_emails = processed_emails
                            extraction_session.total_contacts = total_contacts
                            await db.commit()
                            logger.info(f"Progrès: {processed_emails} emails traités, {total_contacts} contacts valides")
                        except Exception as update_error:
                            logger.error(f"Erreur mise à jour progrès: {update_error}")
                            await db.rollback()
                
                # Finaliser le traitement
                try:
                    await processor.finalize_processing()
                    await processor.deduplicate_contacts()
                except Exception as finalize_error:
                    logger.error(f"Erreur finalisation: {finalize_error}")
                    await db.rollback()
            
            # Marquer comme terminé
            extraction_session.status = "completed"
            extraction_session.date_fin = datetime.utcnow()
            extraction_session.total_emails = processed_emails
            extraction_session.total_contacts = total_contacts
            
            await db.commit()
            logger.info(f"Extraction terminée: {processed_emails} emails, {total_contacts} contacts")
            
        except Exception as e:
            logger.error(f"Erreur globale extraction: {e}")
            # Marquer comme erreur
            if extraction_session:
                try:
                    await db.rollback()
                    extraction_session.status = "error"
                    extraction_session.erreur_message = str(e)
                    extraction_session.date_fin = datetime.utcnow()
                    await db.commit()
                except Exception as commit_error:
                    logger.error(f"Erreur lors de la sauvegarde de l'erreur: {commit_error}")
            raise