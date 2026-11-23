# Public demo on AWS

Infrastructure as code (AWS CDK, Python) for the public demo at https://kcallm.vnmdev.com.

## What it creates

- **One ARM EC2 instance** (`t4g.micro`, Amazon Linux 2023) in the default VPC, with an **Elastic IP**.
- **Security group: ports 80 and 443 only.** There is no SSH; administration uses **SSM Session Manager**.
- **Two containers:** **Caddy** (automatic HTTPS via Let's Encrypt) and the app in **demo mode** (`KCALLM_DEMO=1`).
- **ECR:** `cdk deploy` builds the image locally for ARM and pushes it.
- **SSM Parameter Store:** image URI, model, base URL and domain (created by CDK), plus the **API key as a SecureString** (created by hand, so it never appears in code or templates).

Deliberately absent: a load balancer and a NAT gateway, which would cost more than the instance itself. The demo database is disposable and resets hourly.

## Demo mode

The app pins one hosted OpenAI-compatible vision model, disables ChatGPT sign-in, provider settings and backup restore, limits AI requests per visitor IP per hour and globally per day, and resets to `examples/demo-seed.json` every hour. **Set a hard spend limit on the API key at the provider as well.**

## Deploy

```sh
aws login --region eu-north-1
npx aws-cdk@2 bootstrap                   # once per account/region

# Once: store the model API key (run in your own terminal so the key stays out of logs/history)
read -rs KEY && aws ssm put-parameter --region eu-north-1 \
  --name /kcallm/demo/api-key --type SecureString --value "$KEY" && unset KEY

deploy/aws/deploy.sh <provider/model-id>  # e.g. a vision model with structured-output support
```

Then point a DNS `A` record for `kcallm` at the printed Elastic IP. Caddy obtains the certificate automatically once DNS resolves.

## Operate

```sh
aws ssm start-session --target <instance-id>                 # shell, no SSH
sudo docker logs --tail 100 app                             # app logs
sudo /opt/kcallm/update.sh                                  # re-pull and restart
npx aws-cdk@2 destroy                                       # remove everything
```
