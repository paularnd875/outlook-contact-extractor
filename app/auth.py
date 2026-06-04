from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import RedirectResponse, JSONResponse
import msal
import os
from urllib.parse import urlencode
import httpx
from datetime import datetime, timedelta
from typing import Optional

auth_router = APIRouter()

# Configuration Microsoft Graph
GRAPH_API_ENDPOINT = 'https://graph.microsoft.com/v1.0'
SCOPES = ['Mail.Read', 'User.Read', 'Mail.ReadBasic']

class MSALManager:
    def __init__(self):
        self.client_id = os.getenv('AZURE_CLIENT_ID')
        self.client_secret = os.getenv('AZURE_CLIENT_SECRET')
        self.tenant_id = os.getenv('AZURE_TENANT_ID', 'common')
        self.redirect_uri = os.getenv('AZURE_REDIRECT_URI')
        
        if not all([self.client_id, self.client_secret, self.redirect_uri]):
            raise ValueError("Configuration Azure AD manquante dans les variables d'environnement")
    
    def get_msal_app(self):
        """Crée et retourne une instance MSAL"""
        authority = f'https://login.microsoftonline.com/{self.tenant_id}'
        return msal.ConfidentialClientApplication(
            client_id=self.client_id,
            client_credential=self.client_secret,
            authority=authority
        )
    
    def get_auth_url(self, state: str = None):
        """Génère l'URL d'authentification Microsoft"""
        app = self.get_msal_app()
        auth_url = app.get_authorization_request_url(
            scopes=SCOPES,
            redirect_uri=self.redirect_uri,
            state=state
        )
        return auth_url
    
    def get_token_from_code(self, code: str):
        """Échange le code d'autorisation contre un token"""
        app = self.get_msal_app()
        result = app.acquire_token_by_authorization_code(
            code=code,
            scopes=SCOPES,
            redirect_uri=self.redirect_uri
        )
        return result

# Instance globale du gestionnaire MSAL
msal_manager = MSALManager()

# Stockage temporaire des tokens (en production, utiliser une base de données sécurisée)
user_tokens = {}

@auth_router.get("/login")
async def login(request: Request):
    """Rediriger vers la page de connexion Microsoft"""
    try:
        # Générer un état unique pour la sécurité
        state = f"user_{datetime.now().timestamp()}"
        auth_url = msal_manager.get_auth_url(state=state)
        
        # Stocker l'état dans la session (simplification pour le MVP)
        request.session['auth_state'] = state
        
        return RedirectResponse(url=auth_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors de la génération de l'URL d'authentification: {str(e)}")

@auth_router.get("/callback")
async def auth_callback(request: Request, code: str = None, state: str = None, error: str = None):
    """Traiter le callback d'authentification Microsoft"""
    
    if error:
        raise HTTPException(status_code=400, detail=f"Erreur d'authentification: {error}")
    
    if not code:
        raise HTTPException(status_code=400, detail="Code d'autorisation manquant")
    
    try:
        # Obtenir le token
        token_result = msal_manager.get_token_from_code(code)
        
        if "error" in token_result:
            raise HTTPException(
                status_code=400, 
                detail=f"Erreur lors de l'obtention du token: {token_result.get('error_description', 'Erreur inconnue')}"
            )
        
        # Stocker le token (en production, utiliser une base sécurisée)
        access_token = token_result.get('access_token')
        user_id = await get_user_info(access_token)
        
        user_tokens[user_id] = {
            'access_token': access_token,
            'refresh_token': token_result.get('refresh_token'),
            'expires_at': datetime.now() + timedelta(seconds=token_result.get('expires_in', 3600))
        }
        
        # Rediriger vers le tableau de bord
        return RedirectResponse(url="/dashboard", status_code=302)
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lors du traitement du callback: {str(e)}")

@auth_router.get("/user")
async def get_current_user(token: str = None):
    """Obtenir les informations de l'utilisateur connecté"""
    if not token:
        raise HTTPException(status_code=401, detail="Token manquant")
    
    try:
        user_info = await get_user_info(token)
        return {"user": user_info, "status": "authenticated"}
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Token invalide: {str(e)}")

@auth_router.post("/logout")
async def logout(request: Request):
    """Déconnecter l'utilisateur"""
    # En production, invalider le token côté serveur
    return {"message": "Déconnexion réussie"}

async def get_user_info(access_token: str) -> str:
    """Récupérer les informations utilisateur depuis Microsoft Graph"""
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.get(f'{GRAPH_API_ENDPOINT}/me', headers=headers)
        
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code, 
                detail=f"Erreur lors de la récupération des informations utilisateur: {response.text}"
            )
        
        user_data = response.json()
        return user_data.get('id', user_data.get('userPrincipalName'))

def get_user_token(user_id: str) -> Optional[str]:
    """Récupérer le token d'un utilisateur"""
    user_data = user_tokens.get(user_id)
    if not user_data:
        return None
    
    # Vérifier l'expiration
    if datetime.now() >= user_data['expires_at']:
        # En production, implémenter le refresh token
        return None
    
    return user_data['access_token']