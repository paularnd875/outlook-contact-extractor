#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script de lancement pour l'application Outlook Contact Extractor
"""

import os
import sys
import uvicorn
from pathlib import Path

# Ajouter le répertoire de l'app au PYTHONPATH
app_dir = Path(__file__).parent
sys.path.insert(0, str(app_dir))

if __name__ == "__main__":
    # Vérifier que le fichier .env existe
    env_file = app_dir / ".env"
    if not env_file.exists():
        print("❌ Fichier .env manquant!")
        print("📝 Copiez .env.example vers .env et configurez vos variables Azure AD")
        print("   cp .env.example .env")
        sys.exit(1)
    
    # Charger les variables d'environnement
    from dotenv import load_dotenv
    load_dotenv(env_file)
    
    # Vérifier les variables critiques
    required_vars = [
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_REDIRECT_URI",
        "SECRET_KEY"
    ]
    
    missing_vars = []
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        print("❌ Variables d'environnement manquantes:")
        for var in missing_vars:
            print(f"   - {var}")
        print("\n📝 Configurez ces variables dans votre fichier .env")
        sys.exit(1)
    
    # Configuration du serveur
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", 8000))
    
    print("🚀 Démarrage d'Outlook Contact Extractor")
    print(f"📍 Serveur : http://{host}:{port}")
    print("🔐 Assurez-vous d'avoir configuré votre application Azure AD")
    print("📧 Prêt pour l'extraction de contacts Outlook!\n")
    
    # Démarrer le serveur
    try:
        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            reload=True,
            log_level="info"
        )
    except KeyboardInterrupt:
        print("\n👋 Arrêt de l'application")
    except Exception as e:
        print(f"❌ Erreur lors du démarrage: {e}")
        sys.exit(1)