# 📧 Outlook Contact Extractor

**Outil sécurisé d'extraction de contacts depuis Outlook via Microsoft Graph API**

## 🎯 Description

Cet outil permet d'extraire automatiquement tous les contacts présents dans les emails Outlook (expéditeurs et destinataires) sur une période donnée. Conçu spécialement pour les professionnels (avocats, consultants, etc.) qui ont besoin de reconstituer leurs carnets d'adresses.

## ✨ Fonctionnalités

- **🔐 Authentification sécurisée** via OAuth2 Microsoft
- **📊 Extraction automatique** des contacts depuis tous les dossiers email
- **🔍 Analyse des signatures** pour extraire titres et sites web
- **✅ Validation manuelle** des contacts extraits
- **📋 Prévisualisation** avec filtres et recherche
- **📈 Déduplication intelligente** des contacts
- **📤 Export CSV** avec normalisation des noms
- **⚡ Interface responsive** et intuitive

## 🏗️ Architecture

### Backend
- **FastAPI** - API REST moderne et performante
- **SQLAlchemy** - ORM pour la gestion de base de données
- **Microsoft Graph API** - Accès sécurisé aux emails Outlook
- **MSAL** - Authentification Microsoft

### Frontend
- **Bootstrap 5** - Interface utilisateur responsive
- **JavaScript vanilla** - Interactions dynamiques
- **Axios** - Communication avec l'API

### Base de données
- **SQLite** - Base de données légère pour le développement
- **Extensible** vers PostgreSQL pour la production

## 🚀 Installation et déploiement

### Prérequis

1. **Compte Azure AD** avec permissions d'administration
2. **Python 3.11+**
3. **Git**

### Configuration Azure AD

1. Aller sur le [portail Azure](https://portal.azure.com)
2. Naviguer vers "Azure Active Directory" → "App registrations"
3. Cliquer sur "New registration"
4. Configurer l'application :
   - **Nom** : "Outlook Contact Extractor"
   - **Supported account types** : "Accounts in any organizational directory and personal Microsoft accounts"
   - **Redirect URI** : 
     - Type : Web
     - URL : `https://votre-domaine.railway.app/auth/callback`

5. Noter l'**Application (client) ID**
6. Aller dans "Certificates & secrets" → "New client secret"
7. Noter la **Client secret value**
8. Aller dans "API permissions" → "Add a permission" → "Microsoft Graph" → "Delegated permissions"
9. Ajouter les permissions :
   - `Mail.Read`
   - `Mail.ReadBasic`
   - `User.Read`

### Installation locale

```bash
# Cloner le repository
git clone <repository-url>
cd outlook-contact-extractor

# Créer un environnement virtuel
python -m venv venv
source venv/bin/activate  # Sur Windows: venv\\Scripts\\activate

# Installer les dépendances
pip install -r requirements.txt

# Copier et configurer les variables d'environnement
cp .env.example .env
# Éditer .env avec vos valeurs Azure AD
```

### Variables d'environnement (.env)

```env
# Configuration Azure AD
AZURE_CLIENT_ID=votre_client_id
AZURE_CLIENT_SECRET=votre_client_secret
AZURE_TENANT_ID=common
AZURE_REDIRECT_URI=http://localhost:8000/auth/callback

# Configuration de l'application
SECRET_KEY=votre_clé_secrète_très_longue
DATABASE_URL=sqlite:///./contacts.db

# Configuration du serveur
HOST=0.0.0.0
PORT=8000
```

### Lancement local

```bash
# Démarrer l'application
python -m uvicorn app.main:app --reload

# Accéder à l'application
# http://localhost:8000
```

### Déploiement sur Railway

1. **Pousser le code sur GitHub**
2. **Se connecter sur Railway** (https://railway.app)
3. **Créer un nouveau projet** depuis GitHub
4. **Configurer les variables d'environnement** dans Railway :
   - `AZURE_CLIENT_ID`
   - `AZURE_CLIENT_SECRET`
   - `AZURE_REDIRECT_URI` (avec l'URL Railway)
   - `SECRET_KEY`
   - `DATABASE_URL` (sera automatique)

5. **Déployer** - Railway détectera automatiquement le Dockerfile

## 📖 Utilisation

### 1. Connexion
- Cliquer sur "Se connecter avec Microsoft"
- Autoriser l'application à accéder aux emails
- Redirection automatique vers le tableau de bord

### 2. Extraction
- Sélectionner la période d'extraction (1 mois à 2 ans)
- Cliquer sur "Démarrer l'extraction"
- Suivre le progrès en temps réel

### 3. Validation
- Parcourir les contacts extraits
- Utiliser les filtres (type, validation, recherche)
- Valider/invalider individuellement ou en lot

### 4. Export
- Cliquer sur "Exporter CSV"
- Le fichier contient les colonnes :
  - Nom, Prénom, Nom_Complet
  - Adresse_Mail, Intitulé, Site_Web
  - Nom_Normalisé (pour matching)
  - Statistiques (nombre d'emails, dates)

## 🔧 Normalisation des noms

La normalisation suit cette logique (compatible avec votre script Google Sheets) :
- Conversion en minuscules
- Suppression des accents
- Conservation uniquement des lettres et chiffres
- Format : `prenomnom` (concaténation)

Exemple : `"Jean-François Dupont"` → `"jeanfrancoisdupont"`

## 🛡️ Sécurité

- **OAuth2** : Aucun mot de passe stocké
- **Tokens temporaires** : Expiration automatique
- **Permissions limitées** : Accès lecture seule aux emails
- **Chiffrement HTTPS** : Communication sécurisée
- **Isolation des sessions** : Données séparées par utilisateur

## 📊 Limitations actuelles

- **Débit API** : Microsoft Graph limite à 10,000 requêtes/10min
- **Stockage temporaire** : SQLite pour le MVP (évolutif vers PostgreSQL)
- **Sessions** : Stockage en mémoire (évolutif vers Redis)
- **Authentification** : Une session à la fois par utilisateur

## 🔄 Évolutions prévues

1. **Classification IA** des contacts par métier/secteur
2. **Base de données persistante** (PostgreSQL)
3. **Gestion multi-utilisateurs** avancée
4. **API publique** pour intégrations
5. **Synchronisation bidirectionnelle** avec CRM

## 🐛 Dépannage

### Erreurs courantes

**"Token invalide"**
- Vérifier les credentials Azure AD
- Régénérer le client secret si expiré

**"Permissions insuffisantes"**
- Vérifier que les permissions Graph API sont accordées
- Demander validation admin si nécessaire

**"Erreur de connexion"**
- Vérifier la connectivité internet
- Contrôler les firewalls d'entreprise

### Logs de debug

```bash
# Afficher les logs détaillés
export LOG_LEVEL=DEBUG
python -m uvicorn app.main:app --log-level debug
```

## 📞 Support

Pour toute question technique ou problème :

1. **Vérifier la documentation**
2. **Consulter les logs** d'erreur
3. **Tester les permissions** Azure AD
4. **Contacter le support** si nécessaire

## 📜 License

MIT License - Libre d'utilisation pour projets commerciaux et personnels.

---

**Développé avec ❤️ pour simplifier la gestion des contacts professionnels**