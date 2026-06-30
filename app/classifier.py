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
    return f"""Tu es un assistant d'un cabinet d'avocats. Tu classes un contact à partir de l'historique RÉEL de ses échanges email avec le cabinet.

Catégories possibles (réponds avec l'identifiant exact, en minuscules) :
{cats}

Règles :
- Tiens compte de la RÉCENCE des échanges : un client avec des échanges récents et un dossier en cours = client_actif ; un ancien client dont les derniers échanges sont anciens / dossier clôturé = client_inactif.
- prospect = a pris contact mais n'est pas encore client (pas de dossier ouvert, pas de convention d'honoraires, pas de facture).
- avocat = le contact est lui-même un avocat (confrère/consœur). partenaire = autre professionnel prescripteur (notaire, expert-comptable...).
- Base-toi UNIQUEMENT sur le contenu ci-dessous.

Contact : {ctx['name']} <{ctx['email']}>
Nombre d'échanges : {ctx['count']}
Premier échange : {ctx['first']}  |  Dernier échange : {ctx['last']}

Extraits chronologiques (E = envoyé par le cabinet, R = reçu du contact) :
{ctx['excerpts']}

Réponds UNIQUEMENT en JSON valide :
{{"classification": "<identifiant>", "confiance": <entier 0-100>, "justification": "<1-2 phrases en français citant des éléments concrets du fil>"}}"""


async def classify_contact(ctx: dict, profiles: list) -> dict:
    """Classe un contact. Retourne {classification, confidence, justification}."""
    prompt = build_prompt(ctx, profiles)
    payload = {
        "model": LLM_MODEL,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_predict": 150},
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
    return {"classification": cls, "confidence": conf, "justification": just}
