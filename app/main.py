from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import uvicorn
import os
from dotenv import load_dotenv

from app.auth import auth_router
from app.contacts import contacts_router
from app.ai_profiler import ai_router
from app.enriched_classification import enriched_router
from app.diag import diag_router, setup_log_capture
from app.database import init_db

# Charger les variables d'environnement
load_dotenv()

app = FastAPI(
    title="Outlook Contact Extractor",
    description="Outil d'extraction de contacts depuis Outlook via Microsoft Graph API",
    version="1.0.0"
)

# Ajouter le middleware de session
app.add_middleware(
    SessionMiddleware, 
    secret_key=os.getenv("SECRET_KEY", "fallback-secret-key-for-development-only")
)

# Configuration des templates et fichiers statiques
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Inclusion des routers
app.include_router(auth_router, prefix="/auth", tags=["authentication"])
app.include_router(contacts_router, prefix="/api", tags=["contacts"])
app.include_router(ai_router, prefix="/api/ai", tags=["ai-profiler"])
app.include_router(enriched_router, prefix="/api/ai", tags=["ai-enriched"])
app.include_router(diag_router, prefix="/api", tags=["diag"])

@app.on_event("startup")
async def startup_event():
    """Initialisation de la base de données au démarrage"""
    setup_log_capture()
    await init_db()

def _base_ctx(request: Request) -> dict:
    """Contexte commun aux templates (dont le mode produit avec/sans IA)."""
    from app.enriched_classification import CLASSIFICATION_ENABLED
    return {"request": request, "classification_enabled": CLASSIFICATION_ENABLED}

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Page d'accueil de l'application"""
    return templates.TemplateResponse("index.html", _base_ctx(request))

@app.get("/api/config")
async def get_config():
    """Config produit : mode (extraction seule / + IA) et état du LLM (local ou serveur)."""
    from app.classifier import ping
    from app.enriched_classification import CLASSIFICATION_ENABLED
    llm = await ping() if CLASSIFICATION_ENABLED else {"ok": None, "info": "classification désactivée"}
    return {"classification_enabled": CLASSIFICATION_ENABLED, "llm": llm}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Tableau de bord avec prévisualisation des contacts"""
    return templates.TemplateResponse("dashboard.html", _base_ctx(request))

@app.get("/profils", response_class=HTMLResponse)
async def profils(request: Request):
    """Page de gestion des profils IA (uniquement si la classification est activée)"""
    from app.enriched_classification import CLASSIFICATION_ENABLED
    if not CLASSIFICATION_ENABLED:
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse("profils.html", _base_ctx(request))

@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    return templates.TemplateResponse("error.html", {
        "request": request, 
        "error": "Page non trouvée",
        "status_code": 404
    })

@app.exception_handler(500)
async def server_error_handler(request: Request, exc: HTTPException):
    return templates.TemplateResponse("error.html", {
        "request": request, 
        "error": "Erreur interne du serveur",
        "status_code": 500
    })

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", 8000)),
        reload=True
    )