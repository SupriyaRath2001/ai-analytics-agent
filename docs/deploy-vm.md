# Deploy on a Google Compute Engine VM

One small VM runs everything with Docker Compose: the agent, Open WebUI and Caddy (HTTPS).
An `e2-medium` (2 vCPUs, 4 GB RAM) is a fine start.

You need:

- a GCP project with your BigQuery data,
- a domain name you can add DNS records to (this guide uses `analytics.example.com`),
- an API key for your LLM provider,
- the `gcloud` CLI, for example in Cloud Shell.

Set these once in your shell. The commands below use them.

```bash
PROJECT_ID=your-project-id
REGION=us-central1
ZONE=us-central1-a
SA="analytics-agent@$PROJECT_ID.iam.gserviceaccount.com"
```

## 1. Create a service account for BigQuery

The VM runs as this service account, so no key file is needed.

```bash
gcloud iam service-accounts create analytics-agent \
  --project "$PROJECT_ID" --display-name "Analytics agent"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:$SA" --role roles/bigquery.dataViewer
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:$SA" --role roles/bigquery.jobUser
```

BigQuery Data Viewer lets the agent read tables; BigQuery Job User lets it run queries. To limit
what the agent can read, grant Data Viewer on single datasets instead of the whole project.

## 2. Reserve a static IP and open ports 80 and 443

```bash
gcloud compute addresses create analytics-agent-ip --project "$PROJECT_ID" --region "$REGION"
gcloud compute addresses describe analytics-agent-ip --project "$PROJECT_ID" --region "$REGION" \
  --format "value(address)"

gcloud compute firewall-rules create analytics-agent-web --project "$PROJECT_ID" \
  --network default --target-tags analytics-agent \
  --allow tcp:80,tcp:443,udp:443 --source-ranges 0.0.0.0/0
```

The second command prints the IP address. You need it for DNS in step 4.

## 3. Create the VM

```bash
gcloud compute instances create analytics-agent \
  --project "$PROJECT_ID" --zone "$ZONE" \
  --machine-type e2-medium \
  --image-family debian-13 --image-project debian-cloud \
  --boot-disk-size 30GB \
  --address analytics-agent-ip \
  --tags analytics-agent \
  --service-account "$SA" \
  --scopes cloud-platform
```

- `--boot-disk-size 30GB`: the Open WebUI image is large, so the 10 GB default is too small.
- `--scopes cloud-platform`: lets the VM's service account call BigQuery. What it can do is
  still limited by the two roles from step 1.

## 4. Point your domain at the VM

At your DNS provider, add an **A record** for `analytics.example.com` with the IP address from
step 2. Check it from your own machine:

```bash
nslookup analytics.example.com
```

Wait until it shows the right address. Caddy needs this to get the HTTPS certificate.

## 5. Create a Google OAuth client

This is for "Continue with Google". In the Google Cloud Console, open **Google Auth Platform**:

1. If asked, register the app (app name and support email). Under **Audience**, choose
   **Internal** if all your users are in your Google Workspace: then only they can sign in.
   Otherwise choose **External**. An External app starts in testing mode, where only listed test
   users can sign in, so publish it when you're ready.
2. Go to **Clients**, click **Create client** and choose **Web application**.
3. Under **Authorized redirect URIs**, add `https://analytics.example.com/oauth/google/callback`.
4. Click **Create** and copy the client ID and the client secret. The full secret is shown only
   once.

Changes here can take from 5 minutes to a few hours to take effect.

## 6. Install Docker on the VM

Connect to the VM:

```bash
gcloud compute ssh analytics-agent --project "$PROJECT_ID" --zone "$ZONE"
```

On the VM, install Docker Engine and the Compose plugin. These are the steps from
[Docker's Debian guide](https://docs.docker.com/engine/install/debian/):

```bash
# Add Docker's official GPG key:
sudo apt update
sudo apt install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Add the repository to Apt sources:
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update

sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Then install Git and let your user run `docker` without `sudo`:

```bash
sudo apt install git
sudo usermod -aG docker $USER
exit
```

Connect again with `gcloud compute ssh` (the new group applies from the next login) and check:

```bash
docker compose version
```

## 7. Get the code and fill in the settings

```bash
git clone https://github.com/SupriyaRath2001/ai-analytics-agent.git
cd ai-analytics-agent
cp .env.example .env
cp config/config.yaml.example config/config.yaml
nano .env
```

Set these in `.env`:

| Variable | Value |
|---|---|
| `FAST_MODEL`, `SMART_MODEL`, `LLM_API_KEY` | your LiteLLM model strings and provider key |
| `GCP_PROJECT` | your project ID |
| `GOOGLE_APPLICATION_CREDENTIALS` | leave empty: the VM's service account is used |
| `TZ` | your timezone, e.g. `Asia/Kolkata` (default `UTC`) |
| `DOMAIN` | `analytics.example.com` |
| `WEBUI_URL` | `https://analytics.example.com` |
| `AGENT_API_KEY`, `WEBUI_SECRET_KEY` | two different long random strings, e.g. from `openssl rand -hex 32` |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | from step 5 |
| `PUBLIC_BASE_URL` | leave empty |

`config/config.yaml` works as it is. You may want to set `include_datasets` or
`exclude_datasets`, and check `max_bytes_billed_gb` (the most one query may scan). To teach the
agent your metric definitions, copy `config/business_context.md.example` to
`config/business_context.md` and edit it.

## 8. Start it

```bash
docker compose up -d
docker compose ps
```

The first start builds the agent image and downloads Open WebUI, which takes a few minutes. Caddy
gets the HTTPS certificate once DNS points at the VM.

Open `https://analytics.example.com` and sign in with Google. **The first account to sign in
becomes the admin**, so sign in yourself right away.

When someone else signs in for the first time, their account is **pending**. Approve them in
**Admin Panel → Users** by changing their role from pending to user. They can then chat with
`analytics-agent`.

To see the agent's status (whether table summaries are done, how many tables it knows):

```bash
docker compose exec agent python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').read().decode())"
```

## Updating

```bash
cd ~/ai-analytics-agent
git pull && docker compose up -d --build
```

To also move Open WebUI and Caddy to their latest images:

```bash
docker compose pull open-webui caddy && docker compose up -d
```

Old images use disk space. Remove them with `docker image prune -f`.

## Logs

```bash
docker compose logs -f agent               # follow the agent
docker compose logs --tail 100 open-webui
docker compose logs caddy                  # HTTPS certificate problems show up here
```

## Changing settings later

- After changing `.env`, run `docker compose up -d`. A plain restart does not re-read `.env`.
- After changing `config/config.yaml` or `config/business_context.md`, run
  `docker compose restart agent`.
- Open WebUI saves most of its settings in its own database on first start. Later `.env` changes
  to `WEBUI_URL`, `DEFAULT_MODELS` or the connection key (`AGENT_API_KEY`) do not reach it:
  change them in **Admin Panel → Settings** as well (**Connections** for the key). The Google
  OAuth settings are read from `.env` on every start.

## Troubleshooting

- **Google says `redirect_uri_mismatch`**: the redirect URI in the OAuth client must be exactly
  `https://<your domain>/oauth/google/callback`, and `WEBUI_URL` must be `https://<your domain>`.
- **No HTTPS certificate**: check the DNS record and the firewall rule, then
  `docker compose logs caddy`.
- **The chat has no `analytics-agent` model or answers with an error**: check
  `docker compose logs agent`, for example for a missing `config/config.yaml` or LLM key.
