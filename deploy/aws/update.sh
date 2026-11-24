#!/bin/bash
# Bring the server in line with the latest deployment. Safe to re-run.
#   Usage: update.sh <aws-region>
# Delivered as a CDK asset (S3) and fetched on every deploy, so deploys can change it.
#
#   demo     https://<demo domain>   Caddy -> app            (KCALLM_DEMO=1, resets hourly)
#   private  https://<private domain> Caddy -> oauth2-proxy -> app-private (persistent, backed up)
# The private site is only started once all /kcallm/private/* parameters exist.
set -euo pipefail
REGION="${1:?usage: update.sh <aws-region>}"

param() { aws ssm get-parameter --region "$REGION" --name "$1" "${@:2}" --query Parameter.Value --output text; }
has_param() { aws ssm get-parameter --region "$REGION" --name "$1" >/dev/null 2>&1; }

IMAGE="$(param /kcallm/demo/image-uri)"
DEMO_DOMAIN="$(param /kcallm/demo/domain)"

# A small swap file so image pulls and two app containers fit comfortably in 1 GB of RAM.
if ! swapon --show | grep -q /swapfile; then
  if [ ! -f /swapfile ]; then
    dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
  fi
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap defaults 0 0' >> /etc/fstab
fi

aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${IMAGE%%/*}" >/dev/null
docker pull -q "$IMAGE"
docker network inspect kcallm >/dev/null 2>&1 || docker network create kcallm >/dev/null

run_app() {  # name, data dir, private dir, domain, then extra docker args
  local name="$1" data="$2" private="$3" domain="$4"
  shift 4
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" --network kcallm --restart unless-stopped \
    --read-only --tmpfs /tmp:size=32m --cap-drop ALL --security-opt no-new-privileges \
    -v "$data:/data" -v "$private:/private" \
    -e KCALLM_ALLOWED_ORIGINS="https://$domain" -e KCALLM_ALLOWED_HOSTS="$domain" \
    "$@" "$IMAGE" >/dev/null
}

# ---- Public demo ------------------------------------------------------------
mkdir -p /opt/kcallm/data /opt/kcallm/private
chown 1000:100 /opt/kcallm/data /opt/kcallm/private
run_app app /opt/kcallm/data /opt/kcallm/private "$DEMO_DOMAIN" \
  -e KCALLM_DEMO=1 \
  -e KCALLM_DEMO_MODEL="$(param /kcallm/demo/model)" \
  -e KCALLM_LLAMA_BASE_URL="$(param /kcallm/demo/base-url)" \
  -e KCALLM_LLAMA_API_KEY="$(param /kcallm/demo/api-key --with-decryption)"
CADDYFILE="$(printf '%s {\n\tencode zstd gzip\n\treverse_proxy app:8000\n}\n' "$DEMO_DOMAIN")"

# ---- Private app behind Google sign-in ---------------------------------------
PRIVATE_PARAMS=(domain api-key google-client-id google-client-secret allowed-emails backup-bucket)
private_ready=1
for name in "${PRIVATE_PARAMS[@]}"; do has_param "/kcallm/private/$name" || private_ready=0; done

if [ "$private_ready" = 1 ]; then
  PRIVATE_DOMAIN="$(param /kcallm/private/domain)"
  mkdir -p /opt/kcallm/app-data /opt/kcallm/app-private /opt/kcallm/oauth2
  chown 1000:100 /opt/kcallm/app-data /opt/kcallm/app-private
  chmod 700 /opt/kcallm/app-private /opt/kcallm/oauth2

  run_app app-private /opt/kcallm/app-data /opt/kcallm/app-private "$PRIVATE_DOMAIN" \
    -e KCALLM_OPENROUTER_API_KEY="$(param /kcallm/private/api-key --with-decryption)"

  # The cookie secret is generated once on the server and never leaves it.
  [ -f /opt/kcallm/oauth2/cookie-secret ] || head -c 32 /dev/urandom | base64 | tr -- '+/' '-_' | tr -d '\n' > /opt/kcallm/oauth2/cookie-secret
  param /kcallm/private/allowed-emails | tr ',' '\n' | sed 's/^ *//;s/ *$//' > /opt/kcallm/oauth2/emails
  chmod 600 /opt/kcallm/oauth2/cookie-secret /opt/kcallm/oauth2/emails
  chown -R 65532:65532 /opt/kcallm/oauth2

  # Only Caddy, inside the private Docker network, may set X-Forwarded-* headers.
  NETWORK_CIDR="$(docker network inspect kcallm -f '{{(index .IPAM.Config 0).Subnet}}')"
  docker rm -f oauth2-proxy >/dev/null 2>&1 || true
  docker run -d --name oauth2-proxy --network kcallm --restart unless-stopped \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    -v /opt/kcallm/oauth2:/etc/oauth2:ro \
    -e OAUTH2_PROXY_CLIENT_ID="$(param /kcallm/private/google-client-id)" \
    -e OAUTH2_PROXY_CLIENT_SECRET="$(param /kcallm/private/google-client-secret --with-decryption)" \
    -e OAUTH2_PROXY_COOKIE_SECRET="$(cat /opt/kcallm/oauth2/cookie-secret)" \
    quay.io/oauth2-proxy/oauth2-proxy:v7.15.6 \
    --provider=google \
    --authenticated-emails-file=/etc/oauth2/emails \
    --upstream=http://app-private:8000 \
    --http-address=0.0.0.0:4180 \
    --redirect-url="https://$PRIVATE_DOMAIN/oauth2/callback" \
    --reverse-proxy=true \
    --trusted-proxy-ip="$NETWORK_CIDR" \
    --cookie-secure=true \
    --cookie-samesite=lax \
    --cookie-expire=168h \
    --skip-provider-button=true >/dev/null

  CADDYFILE+="$(printf '\n%s {\n\tencode zstd gzip\n\treverse_proxy oauth2-proxy:4180\n}\n' "$PRIVATE_DOMAIN")"

  # Nightly consistent SQLite backup to S3 (kept 60 days by a bucket lifecycle rule).
  BUCKET="$(param /kcallm/private/backup-bucket)"
  cat > /opt/kcallm/backup.sh <<BACKUP
#!/bin/bash
set -euo pipefail
docker exec app-private /app/.venv/bin/python -c "import sqlite3; s = sqlite3.connect('/data/kcallm.sqlite3'); d = sqlite3.connect('/data/backup.sqlite3'); s.backup(d); d.close()"
aws s3 cp --region "$REGION" --only-show-errors /opt/kcallm/app-data/backup.sqlite3 "s3://$BUCKET/kcallm-\$(date -u +%Y-%m-%dT%H%MZ).sqlite3"
rm -f /opt/kcallm/app-data/backup.sqlite3
BACKUP
  chmod 700 /opt/kcallm/backup.sh
  cat > /etc/systemd/system/kcallm-backup.service <<'UNIT'
[Unit]
Description=Back up the private KcaLLM database to S3
[Service]
Type=oneshot
ExecStart=/opt/kcallm/backup.sh
UNIT
  cat > /etc/systemd/system/kcallm-backup.timer <<'UNIT'
[Unit]
Description=Nightly KcaLLM backup
[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable --now kcallm-backup.timer >/dev/null
  echo "private app: enabled at https://$PRIVATE_DOMAIN"
else
  echo "private app: skipped (missing /kcallm/private/* parameters)"
fi

# ---- Caddy (HTTPS for every site) -------------------------------------------
printf '%s\n' "$CADDYFILE" > /opt/kcallm/Caddyfile
mkdir -p /opt/kcallm/caddy-data /opt/kcallm/caddy-config
if docker inspect caddy >/dev/null 2>&1; then
  docker exec caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null
else
  docker run -d --name caddy --network kcallm --restart unless-stopped \
    -p 80:80 -p 443:443 -p 443:443/udp \
    -v /opt/kcallm/Caddyfile:/etc/caddy/Caddyfile:ro \
    -v /opt/kcallm/caddy-data:/data -v /opt/kcallm/caddy-config:/config \
    caddy:2 >/dev/null
fi
docker image prune -f >/dev/null
echo "update complete"
