# KcaLLM on AWS

Infrastructure as code (AWS CDK, Python) for:

- **the public demo** at https://kcallm.vnmdev.com, and
- **a private copy** at https://app.vnmdev.com behind Google sign-in, for real daily use.

## What it creates

- **One ARM EC2 instance** (`t4g.micro`, Amazon Linux 2023) in the default VPC, with an **Elastic IP**.
- **Security group: ports 80 and 443 only.** There is no SSH; administration uses **SSM Session Manager**.
- **Containers:**
  - **Caddy** provides automatic HTTPS (Let's Encrypt) for both domains.
  - **`app`** runs in **demo mode** (`KCALLM_DEMO=1`).
  - **`oauth2-proxy`** handles Google sign-in and lets in only the addresses in `/kcallm/private/allowed-emails`.
  - **`app-private`** is a normal, persistent instance, reachable only through oauth2-proxy.
- **S3 backup bucket:** the private database is copied there nightly at 03:30 UTC. The bucket is never public, objects expire after 60 days, and it's kept even if the stack is deleted.
- **ECR:** `cdk deploy` builds the image locally for ARM and pushes it.
- **`update.sh`, uploaded as a CDK asset:** the server fetches and runs it on first boot and on every deploy. The first-boot script (`bootstrap.sh`) stays small and stable, because changing it replaces the instance.
- **SSM Parameter Store:**
  - **Created by CDK:** image URI, model, URLs, domains and the bucket name.
  - **Created by hand:** secrets and the allowed email, so they never appear in code or templates.

Deliberately absent: a load balancer and a NAT gateway, which would cost more than the instance itself.

## Demo mode

The app:
- pins one hosted OpenAI-compatible vision model;
- disables ChatGPT sign-in, provider settings and backup restore;
- limits AI requests per visitor IP per hour, and globally per day;
- resets to `examples/demo-seed.json` every hour, opening on a day with some entries already logged.

**Also set a hard spend limit on the API key at the provider.**

## Deploy

```sh
aws login --region eu-north-1
npx aws-cdk@2 bootstrap                   # once per account/region
```

Store the hand-made parameters once. Run these in your own terminal so secrets stay out of logs and history:

```sh
put() { aws ssm put-parameter --region eu-north-1 --name "$1" --type "$2" --value "$3"; }
read -rs KEY && put /kcallm/demo/api-key SecureString "$KEY" && unset KEY        # demo model key

# Private copy: a Google OAuth web client with redirect URI https://app.vnmdev.com/oauth2/callback
read -r  CID && put /kcallm/private/google-client-id String "$CID" && unset CID
read -rs SEC && put /kcallm/private/google-client-secret SecureString "$SEC" && unset SEC
read -rs KEY && put /kcallm/private/api-key SecureString "$KEY" && unset KEY     # its own key and limit
read -r  EML && put /kcallm/private/allowed-emails String "$EML" && unset EML    # comma-separated
```

```sh
deploy/aws/deploy.sh <provider/model-id>  # demo model, e.g. a vision model with structured output
```

Point DNS `A` records for `kcallm` and `app` at the printed Elastic IP. Caddy obtains the certificates automatically once DNS resolves. The private site is only started once all `/kcallm/private/*` parameters exist.

## Operate

```sh
aws ssm start-session --target <instance-id>                 # shell, no SSH
sudo docker logs --tail 100 app-private                     # private app logs (or: app, oauth2-proxy, caddy)
sudo /opt/kcallm/backup.sh                                  # back up the private database now
sudo systemctl list-timers kcallm-backup.timer              # next scheduled backup
aws s3 ls s3://<backup-bucket>/                             # list backups
npx aws-cdk@2 destroy                                       # remove everything except the backup bucket
```

To restore a backup, download a `.sqlite3` file from the bucket, stop `app-private`, delete any `kcallm.sqlite3-wal` and `kcallm.sqlite3-shm` files in `/opt/kcallm/app-data/` (the database uses WAL mode), copy the backup over `kcallm.sqlite3` (owned by `1000:100`), and start the container again. Day to day, the app's own **Export** and **Restore** in Settings are simpler.
