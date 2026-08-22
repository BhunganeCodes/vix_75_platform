#!/bin/bash
# =====================================================================
#  VIX75 Platform - Oracle Cloud ARM (Always Free) provisioning
#
#  Target: Ubuntu 22.04 (A1.Flex, 4 OCPU / 24 GB)
#
#  SECURITY MODEL (audit fixes baked in):
#    * Firewall is managed EXCLUSIVELY via UFW - no blanket iptables
#      ACCEPT rules, nothing beyond SSH/HTTP/HTTPS is reachable.
#    * Postgres/Redis/internal services get NO public ports; only Caddy
#      terminates traffic on 80/443 and proxies to api-gateway.
#    * Secrets are NOT handled here. Place them manually at
#      /opt/vix75/.env (chmod 600) after this script runs.
#
#  Usage:
#    chmod +x deploy/oracle/setup.sh && sudo ./deploy/oracle/setup.sh
# =====================================================================
set -euo pipefail

APP_DIR="/opt/vix75"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

[[ ${EUID} -eq 0 ]] || { echo "ERROR: run as root (sudo)" >&2; exit 1; }

# ---------------------------------------------------------------------
log "[1/7] System packages"
# ---------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq ca-certificates curl git ufw

# ---------------------------------------------------------------------
log "[2/7] Docker Engine + Compose plugin (ARM64)"
# ---------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc
    echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu jammy stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
docker version --format 'docker {{.Server.Version}}'
docker compose version

# ---------------------------------------------------------------------
log "[3/7] UFW firewall (deny-by-default; ONLY ssh/80/443)"
# ---------------------------------------------------------------------
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 80/tcp comment 'Caddy HTTP (ACME + redirect)'
ufw allow 443/tcp comment 'Caddy HTTPS'
ufw --force enable
ufw status verbose

# NOTE: we deliberately do NOT touch raw iptables here (the legacy setup
# script flushed INPUT to ACCEPT - that class of mistake is banned), and
# ports like 5432/8080/9090/3000 stay internal to the compose network.

# ---------------------------------------------------------------------
log "[4/7] App directory ${APP_DIR}"
# ---------------------------------------------------------------------
mkdir -p "${APP_DIR}"/{models,logs,caddy/data,caddy/config}
chmod 750 "${APP_DIR}"

if [[ -f "$(pwd)/docker-compose.prod.yml" ]]; then
    cp docker-compose.prod.yml "${APP_DIR}/docker-compose.yml"
    cp deploy/oracle/Caddyfile "${APP_DIR}/Caddyfile"
    mkdir -p "${APP_DIR}/infra/timescale" "${APP_DIR}/infra/redis"
    [[ -f infra/timescale/schema.sql ]] && cp infra/timescale/schema.sql "${APP_DIR}/infra/timescale/"
    [[ -f infra/redis/redis.conf ]] && cp infra/redis/redis.conf "${APP_DIR}/infra/redis/"
else
    echo "WARNING: run this script from the repository root so compose/Caddyfile can be copied."
fi

# ---------------------------------------------------------------------
log "[5/7] Secrets placeholder (/opt/vix75/.env)"
# ---------------------------------------------------------------------
if [[ ! -f "${APP_DIR}/.env" ]]; then
    cat > "${APP_DIR}/.env" <<'ENV'
# Fill in REAL values out-of-band (scp / vault). chmod 600 required.
DRY_RUN_MODE=true
MT5_LOGIN=0
MT5_PASSWORD=change-me
MT5_SERVER=DerivSVG-Server-03
DERIV_API_TOKEN=change-me
TELEGRAM_TOKEN=change-me
TELEGRAM_CHAT_ID=0
GATEWAY_USERNAME=vix-admin
GATEWAY_PASSWORD=change-me-gateway
JWT_SECRET=change-me-32-bytes-minimum-secret!
ENV
    chmod 600 "${APP_DIR}/.env"
    echo "Placeholder .env created - EDIT IT NOW with real values."
else
    chmod 600 "${APP_DIR}/.env"
fi

# ---------------------------------------------------------------------
log "[6/7] Log rotation for compose json logs"
# ---------------------------------------------------------------------
cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "5" }
}
JSON
systemctl restart docker

# ---------------------------------------------------------------------
log "[7/7] Done"
# ---------------------------------------------------------------------
cat <<'NEXT'

Next steps:
  1. Edit /opt/vix75/.env (real secrets; keep DRY_RUN_MODE=true).
  2. Point api.yourdomain.com DNS A record at this VM's public IP.
  3. cd /opt/vix75 && docker compose up -d
  4. Watch Caddy obtain certificates: docker compose logs -f caddy
  5. Smoke test: curl https://api.yourdomain.com/health

The stack ships in DRY_RUN_MODE=true: order requests produce simulated
fills through the full pipeline (DB rows, Telegram, metrics) with zero
broker risk. Flip DRY_RUN_MODE=false ONLY after the validation window.

NEXT
