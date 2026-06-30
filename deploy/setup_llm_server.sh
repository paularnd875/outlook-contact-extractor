#!/usr/bin/env bash
# =============================================================================
# Installe un SERVICE LLM réutilisable (Ollama + Qwen2.5 14B) sur une instance
# GPU Ubuntu 22.04 (Scaleway / OVHcloud, France — RGPD).
#
# Objectif : exposer une URL HTTPS sécurisée par clé d'API, que l'outil
# (et d'autres plateformes clientes) consommera via :
#     CLASSIFIER_BACKEND=remote
#     LLM_URL=https://<DOMAINE>
#     LLM_API_KEY=<la clé ci-dessous>
#
# Usage (sur la VM fraîche, en root) :
#     LLM_DOMAIN=llm.mondomaine.fr LLM_API_KEY="$(openssl rand -hex 32)" \
#         bash setup_llm_server.sh
#
# Si vous ne mettez PAS de domaine (LLM_DOMAIN vide) : le script installe
# seulement Ollama en local et vous indique l'option Tailscale (réseau privé).
# =============================================================================
set -euo pipefail

MODEL="${MODEL:-qwen2.5:14b}"
LLM_DOMAIN="${LLM_DOMAIN:-}"
LLM_API_KEY="${LLM_API_KEY:-}"

echo ">>> 1/6  Mise à jour système"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y && apt-get upgrade -y
apt-get install -y curl ufw openssl

echo ">>> 2/6  Vérification du GPU NVIDIA"
if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi
else
    echo "⚠️  nvidia-smi absent. Utilisez une IMAGE OS « GPU » du fournisseur"
    echo "    (drivers NVIDIA préinstallés). Sinon Ollama tournera sur CPU (lent)."
fi

echo ">>> 3/6  Installation d'Ollama (natif Linux)"
curl -fsSL https://ollama.com/install.sh | sh

echo ">>> 4/6  Config Ollama : écoute LOCALE seulement + parallélisme"
mkdir -p /etc/systemd/system/ollama.service.d
cat >/etc/systemd/system/ollama.service.d/override.conf <<EOF
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_NUM_PARALLEL=4"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
EOF
systemctl daemon-reload
systemctl enable ollama
systemctl restart ollama
sleep 4

echo ">>> 5/6  Téléchargement du modèle : $MODEL"
ollama pull "$MODEL"

echo ">>> 6/6  Pare-feu + accès distant sécurisé"
ufw allow 22/tcp >/dev/null
if [ -n "$LLM_DOMAIN" ] && [ -n "$LLM_API_KEY" ]; then
    echo "    -> Reverse-proxy HTTPS (Caddy) + clé d'API sur https://$LLM_DOMAIN"
    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get update -y && apt-get install -y caddy

    cat >/etc/caddy/Caddyfile <<EOF
$LLM_DOMAIN {
    @authorized header Authorization "Bearer $LLM_API_KEY"
    handle @authorized {
        reverse_proxy 127.0.0.1:11434
    }
    respond "Unauthorized" 401
}
EOF
    systemctl restart caddy
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
    echo "    ✅ Le port 11434 n'est PAS exposé (Caddy seul est public, sur 443)."
else
    echo "    -> Pas de domaine fourni : Ollama reste en local (127.0.0.1)."
    echo "       Option RECOMMANDÉE = réseau privé Tailscale (aucune exposition publique) :"
    echo "         curl -fsSL https://tailscale.com/install.sh | sh && tailscale up"
    echo "       puis sur la VM : OLLAMA_HOST=0.0.0.0 (override) et connectez l'app"
    echo "       via l'IP Tailscale du serveur (http://<ip-tailscale>:11434)."
fi
ufw --force enable

echo ""
echo "=============================================================="
echo " SERVICE LLM PRÊT"
echo "   Modèle      : $MODEL"
if [ -n "$LLM_DOMAIN" ]; then
echo "   URL         : https://$LLM_DOMAIN"
echo "   Clé d'API   : $LLM_API_KEY"
echo ""
echo " Test depuis votre Mac :"
echo "   curl -H \"Authorization: Bearer $LLM_API_KEY\" https://$LLM_DOMAIN/api/version"
fi
echo "=============================================================="
