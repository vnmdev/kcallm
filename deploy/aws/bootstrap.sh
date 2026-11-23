#!/bin/bash
# First-boot setup for the KcaLLM demo instance (Amazon Linux 2023, ARM).
# Installs Docker, writes the Caddy config and an update script, then starts both containers.
set -euxo pipefail

dnf install -y docker
systemctl enable --now docker

mkdir -p /opt/kcallm/data /opt/kcallm/private /opt/kcallm/caddy-data /opt/kcallm/caddy-config
chown 1000:100 /opt/kcallm/data /opt/kcallm/private
chmod 700 /opt/kcallm/private

cat > /opt/kcallm/update.sh <<'UPDATE'
#!/bin/bash
# Pull the image recorded in SSM and (re)start the app; start Caddy if it isn't running.
# Safe to re-run: deploy/aws/deploy.sh runs it through SSM after each `cdk deploy`.
set -euo pipefail
REGION="__REGION__"
param() { aws ssm get-parameter --region "$REGION" --name "/kcallm/demo/$1" "${@:2}" --query Parameter.Value --output text; }
IMAGE="$(param image-uri)"
MODEL="$(param model)"
BASE_URL="$(param base-url)"
DOMAIN="$(param domain)"
API_KEY="$(param api-key --with-decryption)"

printf '%s {\n\tencode zstd gzip\n\treverse_proxy app:8000\n}\n' "$DOMAIN" > /opt/kcallm/Caddyfile

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${IMAGE%%/*}"
docker pull "$IMAGE"
docker network inspect kcallm >/dev/null 2>&1 || docker network create kcallm

docker rm -f app >/dev/null 2>&1 || true
docker run -d --name app --network kcallm --restart unless-stopped \
  --read-only --tmpfs /tmp:size=32m --cap-drop ALL --security-opt no-new-privileges \
  -v /opt/kcallm/data:/data -v /opt/kcallm/private:/private \
  -e KCALLM_DEMO=1 \
  -e KCALLM_DEMO_MODEL="$MODEL" \
  -e KCALLM_LLAMA_BASE_URL="$BASE_URL" \
  -e KCALLM_LLAMA_API_KEY="$API_KEY" \
  -e KCALLM_ALLOWED_ORIGINS="https://$DOMAIN" \
  -e KCALLM_ALLOWED_HOSTS="$DOMAIN" \
  "$IMAGE"

if ! docker inspect caddy >/dev/null 2>&1; then
  docker run -d --name caddy --network kcallm --restart unless-stopped \
    -p 80:80 -p 443:443 -p 443:443/udp \
    -v /opt/kcallm/Caddyfile:/etc/caddy/Caddyfile:ro \
    -v /opt/kcallm/caddy-data:/data -v /opt/kcallm/caddy-config:/config \
    caddy:2
else
  docker exec caddy caddy reload --config /etc/caddy/Caddyfile
fi
docker image prune -f >/dev/null
UPDATE
chmod 700 /opt/kcallm/update.sh

/opt/kcallm/update.sh
