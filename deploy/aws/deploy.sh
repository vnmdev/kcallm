#!/bin/bash
# Build, push and roll out KcaLLM (demo + private): `deploy/aws/deploy.sh <provider/model-id>`
# Requires: `aws login`, Docker running, and the SecureString /kcallm/demo/api-key.
set -euo pipefail
cd "$(dirname "$0")"
MODEL="${1:?usage: deploy.sh <provider/model-id>}"
REGION="$(jq -r .context.region cdk.json)"

aws sts get-caller-identity >/dev/null 2>&1 \
  || { echo "AWS sign-in missing or expired: run 'aws login --region $REGION'"; exit 1; }
aws ssm get-parameter --region "$REGION" --name /kcallm/demo/api-key >/dev/null 2>&1 \
  || { echo "Create the API key first: see deploy/aws/README.md"; exit 1; }

npx --yes aws-cdk@2 deploy --require-approval never -c model="$MODEL" --outputs-file cdk-outputs.json
INSTANCE="$(jq -r '.KcaLLMDemo.InstanceId' cdk-outputs.json)"

# Fetch the current update.sh (a CDK asset) and run it: pulls the new image, restarts the
# containers and enables the private app once its parameters exist.
for _ in $(seq 36); do
  STATUS="$(aws ssm describe-instance-information --region "$REGION" \
    --filters "Key=InstanceIds,Values=$INSTANCE" --query 'InstanceInformationList[0].PingStatus' --output text)"
  [ "$STATUS" = Online ] && break
  echo "Waiting for the server to come online ($STATUS)..."; sleep 5
done
SCRIPT_URL="$(aws ssm get-parameter --region "$REGION" --name /kcallm/demo/update-script --query Parameter.Value --output text)"
RUN="aws s3 cp --region $REGION --only-show-errors $SCRIPT_URL /opt/kcallm/update.sh && chmod 700 /opt/kcallm/update.sh && /opt/kcallm/update.sh $REGION"
COMMAND_ID="$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE" \
  --document-name AWS-RunShellScript --parameters "$(jq -cn --arg c "$RUN" '{commands: [$c]}')" \
  --query Command.CommandId --output text)"
aws ssm wait command-executed --region "$REGION" --command-id "$COMMAND_ID" --instance-id "$INSTANCE" || true
aws ssm get-command-invocation --region "$REGION" --command-id "$COMMAND_ID" --instance-id "$INSTANCE" \
  --query '[Status, StandardOutputContent, StandardErrorContent]' --output text | grep -v 'password will be stored\|credential helper\|credentials-store'
jq -r '.KcaLLMDemo | "Demo:    \(.Url)\nPrivate: \(.PrivateUrl)"' cdk-outputs.json
