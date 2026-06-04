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

@app.on_event("startup")
async def startup_event():
    """Initialisation de la base de données au démarrage"""
    await init_db()

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Page d'accueil de l'application"""
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Tableau de bord avec prévisualisation des contacts"""
    return templates.TemplateResponse("dashboard.html", {"request": request})

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