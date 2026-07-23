"""
Classifieur de contacts par LLM, à backend débranchable.

Choix du backend via variables d'environnement (.env) :
  CLASSIFIER_BACKEND = local | remote | api   (par défaut: local)
  LLM_URL            = http://localhost:11434  (local) ou l'URL de votre serveur (remote)
  LLM_MODEL          = qwen2.5:14b

local et remote utilisent le même code (API Ollama) : seule l'URL change.
Aucune donnée d'email n'est stockée ici : tout reste en mémoire le temps de l'appel.
"""

import os
import json
import logging
import httpx

logger = logging.getLogger(__name__)

CLASSIFIER_BACKEND = os.getenv("CLASSIFIER_BACKEND", "local")
LLM_URL = os.getenv("LLM_URL", "http://localhost:11434")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:14b")
# Clé d'API optionnelle pour sécuriser un LLM distant (reverse-proxy / serveur).
# Vide en local ; à renseigner quand le modèle tourne sur un serveur.
LLM_API_KEY = os.getenv("LLM_API_KEY", "")


def _llm_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        h["Authorization"] = f"Bearer {LLM_API_KEY}"
    return h


async def ping() -> dict:
    """Vérifie que le LLM (local ou distant) répond. Pour diagnostiquer le basculement serveur."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{LLM_URL}/api/version", headers=_llm_headers())
            r.raise_for_status()
            return {"ok": True, "backend": CLASSIFIER_BACKEND, "url": LLM_URL,
                    "model": LLM_MODEL, "version": r.json().get("version")}
    except Exception as e:
        return {"ok": False, "backend": CLASSIFIER_BACKEND, "url": LLM_URL, "error": str(e)}


def build_prompt(ctx: dict, profiles: list) -> str:
    """Construit le prompt de classification à partir des catégories et du fil d'échanges."""
    cats = "\n".join(f"- {p['id']} : {p['description']}" for p in profiles)
    owner = ctx.get("owner_name") or "le cabinet"
    owner_email = ctx.get("owner_email") or ""
    email = ctx["email"]
    domain = email.split("@")[-1] if "@" in email else ""
    excerpts = (ctx.get("excerpts") or "").strip() or "(Aucun échange lisible récupéré — classe au mieux d'après le NOM, l'EMAIL et le DOMAINE ci-dessus.)"
    return f"""Tu es un assistant du cabinet d'avocats de {owner}. Tu classes UN CONTACT de ce cabinet.

⚠️ TRÈS IMPORTANT : la boîte email analysée appartient à {owner} <{owner_email}>. Tu dois classer LE CONTACT ci-dessous (« {ctx['name']} <{email}> »), et SURTOUT PAS la propriétaire {owner}. La signature de {owner} apparaît dans presque tous les emails : IGNORE-la, ce n'est jamais le contact. Ne dis jamais que le contact « se présente comme {owner} ».

Catégories possibles (réponds avec l'identifiant exact, en minuscules) :
{cats}

Règles :
- RÉCENCE : échanges récents + dossier en cours = client_actif ; ancien client / dossier clôturé = client_inactif.
- prospect = a pris contact mais pas encore client (ni dossier, ni convention d'honoraires, ni facture).
- avocat = le contact est lui-même avocat (confrère/consœur/collaborateur). partenaire = autre professionnel prescripteur (notaire, expert-comptable, huissier...).
- LE DOMAINE DE L'EMAIL est un indice fort, surtout si le fil est absent ou peu concluant :
    · domaine de cabinet d'avocats (ex. contient « avocat(s) », « barreau », « law ») → **avocat**
    · « notaire(s) » → **partenaire**
    · gmail/outlook/hotmail/free/yahoo → un particulier (client/prospect selon le contenu, sinon autre)
  Ici, domaine du contact = « {domain} ».
- Ne mets « autre » QUE si vraiment rien n'est exploitable (ni fil, ni indice de domaine/nom).
- IDENTITÉ : si le PRÉNOM et le NOM de la personne apparaissent clairement (signature « Cordialement, X Y », formule de politesse, ou motif de l'adresse email type « prenom.nom@ » / « p.nom@ »), renseigne « prenom » et « nom ». Si c'est une adresse générique ou une société (accueil@, info@, contact@, no-reply, nom de société sans personne), LAISSE « prenom » et « nom » VIDES. N'invente jamais un prénom à partir d'une simple initiale.

Contact : {ctx['name']} <{email}>
Nombre total d'emails échangés : {ctx['count']}
Premier échange : {ctx['first']}  |  Dernier échange : {ctx['last']}

Extraits chronologiques (E = envoyé par le cabinet, R = reçu du contact) :
{excerpts}

Réponds UNIQUEMENT en JSON valide :
{{"classification": "<identifiant>", "confiance": <entier 0-100>, "justification": "<1-2 phrases en français ; parle du CONTACT, jamais de {owner}>", "prenom": "<prénom du contact ou vide>", "nom": "<nom de famille du contact ou vide>"}}"""


async def classify_contact(ctx: dict, profiles: list) -> dict:
    """Classe un contact. Retourne {classification, confidence, justification}."""
    prompt = build_prompt(ctx, profiles)
    payload = {
        "model": LLM_MODEL,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_predict": 220},
        "prompt": prompt,
    }
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(f"{LLM_URL}/api/generate", json=payload, headers=_llm_headers())
            r.raise_for_status()
            raw = r.json().get("response", "{}")
        data = json.loads(raw)
    except Exception as e:
        logger.error(f"Erreur classification LLM pour {ctx.get('email')}: {e}")
        return {"classification": "autre", "confidence": 0,
                "justification": "Échec de la classification automatique."}

    valid_ids = {p["id"] for p in profiles}
    cls = str(data.get("classification", "")).strip().lower()
    if cls not in valid_ids:
        cls = "autre"
    try:
        conf = max(0, min(100, int(data.get("confiance", data.get("confidence", 50)))))
    except (ValueError, TypeError):
        conf = 50
    just = str(data.get("justification", "")).strip()[:600]

    from app.normalizer import clean_person_name
    def _clean_name(v):
        v = str(v or "").strip()
        # placeholders explicites
        if not v or v.lower() in ("vide", "n/a", "inconnu", "none", "-"):
            return ""
        # nettoyage fioritures (titres, parenthèses, email, guillemets)
        return (clean_person_name(v) or "")[:60]

    return {"classification": cls, "confidence": conf, "justification": just,
            "prenom": _clean_name(data.get("prenom")), "nom": _clean_name(data.get("nom"))}
