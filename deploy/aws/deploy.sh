#!/bin/bash
# Build, push and roll out the demo: `deploy/aws/deploy.sh <provider/model-id>`
# Requires: `aws login`, Docker running, and the SecureString /kcallm/demo/api-key.
set -euo pipefail
cd "$(dirname "$0")"
MODEL="${1:?usage: deploy.sh <provider/model-id>}"
REGION="$(jq -r .context.region cdk.json)"

aws ssm get-parameter --region "$REGION" --name /kcallm/demo/api-key >/dev/null \
  || { echo "Create the API key first: see deploy/aws/README.md"; exit 1; }

npx --yes aws-cdk@2 deploy --require-approval never -c model="$MODEL" --outputs-file cdk-outputs.json
INSTANCE="$(jq -r '.KcaLLMDemo.InstanceId' cdk-outputs.json)"

# Existing instances only run their bootstrap once, so pull the new image explicitly.
COMMAND_ID="$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE" \
  --document-name AWS-RunShellScript --parameters 'commands=["/opt/kcallm/update.sh"]' \
  --query Command.CommandId --output text 2>/dev/null || true)"
if [ -n "$COMMAND_ID" ]; then
  aws ssm wait command-executed --region "$REGION" --command-id "$COMMAND_ID" --instance-id "$INSTANCE" || true
  aws ssm get-command-invocation --region "$REGION" --command-id "$COMMAND_ID" \
    --instance-id "$INSTANCE" --query '{status: Status, error: StandardErrorContent}' --output table
fi
jq -r '.KcaLLMDemo | "IP: \(.ElasticIp)\nURL: \(.Url)"' cdk-outputs.json
