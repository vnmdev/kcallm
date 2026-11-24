#!/bin/bash
# First-boot setup for the KcaLLM server (Amazon Linux 2023, ARM): install Docker, then
# fetch and run update.sh, the same script deploy/aws/deploy.sh runs on every deploy.
# Kept small and stable: changing user data restarts the instance, changing update.sh doesn't.
set -euxo pipefail
REGION="__REGION__"

dnf install -y docker
systemctl enable --now docker

mkdir -p /opt/kcallm
SCRIPT_URL="$(aws ssm get-parameter --region "$REGION" --name /kcallm/demo/update-script --query Parameter.Value --output text)"
aws s3 cp --region "$REGION" "$SCRIPT_URL" /opt/kcallm/update.sh
chmod 700 /opt/kcallm/update.sh
/opt/kcallm/update.sh "$REGION"
