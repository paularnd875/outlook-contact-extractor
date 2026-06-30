# Guide — Service LLM sur instance GPU (EU / RGPD)

Objectif : faire tourner le modèle de classification **sur un serveur loué en France**
(et plus sur le Mac), exposé comme une **URL sécurisée réutilisable** par l'outil et,
demain, par d'autres plateformes clientes.

Branchement final côté app (fichier `.env`) :
```
CLASSIFIER_BACKEND=remote
LLM_URL=https://llm.mondomaine.fr
LLM_API_KEY=<clé générée à l'install>
```

---

## 1. Choisir l'instance GPU (France)

Une carte **NVIDIA L4 (24 Go)** suffit largement pour Qwen2.5 14B.

| Fournisseur | Type d'instance conseillé | GPU | Indicatif |
|---|---|---|---|
| **Scaleway** (Paris) | `L4-1-24G` | L4 24 Go | ~0,75–1 €/h |
| **OVHcloud** (Gravelines/Roubaix) | `l4-90` (ou `t1-le-45`) | L4 / V100 | ~0,90–1,2 €/h |

Conseils :
- **OS** : choisir une **image « GPU » Ubuntu 22.04** (drivers NVIDIA préinstallés).
- **Allumage à la demande** : on peut éteindre l'instance entre deux campagnes de
  classification → coût réel ~**1 € par passe** de ~700 contacts.
- Stockage : 40–60 Go suffisent (le modèle fait ~9 Go).

## 2. (Option A — public) Préparer un sous-domaine

Si vous exposez via HTTPS public : créez un enregistrement DNS **A**
`llm.mondomaine.fr → <IP publique de la VM>`. Ouvrez les ports 80 et 443.

> **Option B — privé (recommandé si pas de domaine)** : pas de DNS, pas de port public.
> On relie le serveur et votre Mac via **Tailscale** (réseau privé chiffré WireGuard).
> Voir l'étape 4.

## 3. Lancer l'installation

Connectez-vous en SSH à la VM, copiez `setup_llm_server.sh`, puis :

**Option A (domaine + clé d'API) :**
```bash
LLM_DOMAIN=llm.mondomaine.fr LLM_API_KEY="$(openssl rand -hex 32)" \
    bash setup_llm_server.sh
```
Le script installe Ollama + le modèle, le met en écoute **locale uniquement**, et place
un reverse-proxy **Caddy** (HTTPS auto) qui exige la clé d'API. **Notez la clé affichée à la fin.**

**Option B (privé / Tailscale) :**
```bash
bash setup_llm_server.sh                       # installe Ollama + modèle
curl -fsSL https://tailscale.com/install.sh | sh && tailscale up
```
Puis l'URL côté app sera `http://<ip-tailscale-serveur>:11434` (sans clé, réseau privé).

## 4. Brancher l'outil

Dans le `.env` de l'app (sur le Mac aujourd'hui, sur le futur serveur d'app demain) :
```
CLASSIFIER_BACKEND=remote
LLM_URL=https://llm.mondomaine.fr      # ou http://<ip-tailscale>:11434
LLM_API_KEY=<clé>                      # vide si Tailscale
```
Vérifier le branchement :
```
curl http://127.0.0.1:8000/api/config   # doit afficher llm.ok = true
```

## 5. Sécurité / RGPD — points de contrôle

- ✅ Port **11434 jamais exposé** : seul Caddy (443, avec clé) ou Tailscale (privé) est accessible.
- ✅ Données en **UE** (Scaleway/OVH France).
- ✅ Aucun contenu d'email stocké sur le serveur LLM (Ollama ne persiste pas les prompts).
- 🔒 Restreindre l'accès SSH (clé uniquement), garder l'OS à jour.
- 📄 Prévoir un **contrat de sous-traitance (DPA)** avec le fournisseur (légal RGPD).

## 6. Le jour où on bascule pour de bon

Une fois `/api/config` qui répond `llm.ok=true` vers le serveur :
- on désinstalle Ollama du Mac (`ollama rm qwen2.5:14b`, suppression de l'app, `~/.ollama`) → libère ~9 Go.
