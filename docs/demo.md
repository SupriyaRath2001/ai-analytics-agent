# Demo: see it working in about 20 minutes

This runs the whole app (agent, chat UI, proxy) on a small Google Cloud VM, against a copy of
Google's public demo shop data, TheLook eCommerce: orders, order items, products, users and more.
You only need a browser: every command runs in [Cloud Shell](https://shell.cloud.google.com) or on
the VM.

You need:

- the code on GitHub (push it first),
- a GCP project with billing turned on (the VM needs it),
- an API key for an LLM: OpenAI, Gemini, Anthropic, or your company's OpenAI-compatible gateway.

## 1. Copy the demo data into your project

In the BigQuery console, open a new query tab and run:

```sql
CREATE SCHEMA IF NOT EXISTS demo_shop OPTIONS (location = 'US');

CREATE OR REPLACE TABLE demo_shop.orders AS
  SELECT * FROM `bigquery-public-data.thelook_ecommerce.orders`;
CREATE OR REPLACE TABLE demo_shop.order_items AS
  SELECT * FROM `bigquery-public-data.thelook_ecommerce.order_items`;
CREATE OR REPLACE TABLE demo_shop.products AS
  SELECT * FROM `bigquery-public-data.thelook_ecommerce.products`;
CREATE OR REPLACE TABLE demo_shop.users AS
  SELECT * FROM `bigquery-public-data.thelook_ecommerce.users`;
CREATE OR REPLACE TABLE demo_shop.inventory_items AS
  SELECT * FROM `bigquery-public-data.thelook_ecommerce.inventory_items`;
CREATE OR REPLACE TABLE demo_shop.distribution_centers AS
  SELECT * FROM `bigquery-public-data.thelook_ecommerce.distribution_centers`;
```

That gives you 6 small tables in a dataset called `demo_shop`.

For a richer test set, run [`scripts/demo_data.sql`](../scripts/demo_data.sql) instead. It has the
same 6 tables plus website clicks (`events`), two helper tables (clean country names and monthly
revenue targets), table descriptions and monthly partitions. In step 5 you then wait for
`curated 9/9 tables`.

## 2. Create the VM (in Cloud Shell)

```bash
PROJECT_ID=your-project-id
ZONE=asia-south1-a   # pick a zone near you
SA="analytics-agent@$PROJECT_ID.iam.gserviceaccount.com"

# A service account that may read BigQuery and run queries
gcloud iam service-accounts create analytics-agent --project "$PROJECT_ID"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:$SA" --role roles/bigquery.dataViewer
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:$SA" --role roles/bigquery.jobUser

# Open the web ports
gcloud compute firewall-rules create analytics-agent-web --project "$PROJECT_ID" \
  --network default --target-tags analytics-agent \
  --allow tcp:80,tcp:443,udp:443 --source-ranges 0.0.0.0/0

# The VM itself
gcloud compute instances create analytics-agent \
  --project "$PROJECT_ID" --zone "$ZONE" \
  --machine-type e2-medium \
  --image-family debian-13 --image-project debian-cloud \
  --boot-disk-size 30GB \
  --tags analytics-agent \
  --service-account "$SA" --scopes cloud-platform

# Its public IP address: you need it in step 4
gcloud compute instances describe analytics-agent --project "$PROJECT_ID" --zone "$ZONE" \
  --format "get(networkInterfaces[0].accessConfigs[0].natIP)"
```

## 3. Install Docker and get the code (on the VM)

Connect:

```bash
gcloud compute ssh analytics-agent --project "$PROJECT_ID" --zone "$ZONE"
```

On the VM, install Docker, the Compose plugin and Git (the same commands as in
[deploy-vm.md](deploy-vm.md), step 6):

```bash
sudo apt update
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
exit
```

Connect again with the same `gcloud compute ssh` command, then:

```bash
git clone https://github.com/SupriyaRath2001/ai-analytics-agent.git
cd ai-analytics-agent
cp .env.example .env
cp config/config.yaml.example config/config.yaml
```

## 4. Fill in the settings

Run `nano .env` and set:

| Variable | Demo value |
|---|---|
| `FAST_MODEL`, `SMART_MODEL` | your models as LiteLLM strings, e.g. `openai/<model>` or `gemini/<model>` |
| `LLM_API_KEY` | your key |
| `LLM_API_BASE` | only for a company gateway: its URL. The models are then `openai/<model name on the gateway>` |
| `GCP_PROJECT` | your project ID |
| `TZ` | your timezone, e.g. `Asia/Kolkata` |
| `DOMAIN` | `http://<VM IP>` |
| `WEBUI_URL` | `http://<VM IP>` |
| `AGENT_API_KEY`, `WEBUI_SECRET_KEY` | two different values from `openssl rand -hex 32` |

Leave the Google login values empty. For the demo you sign in with an email and password.

Then run `nano config/config.yaml` and set `include_datasets: [demo_shop]`.

Optionally, add two business rules so you can see them used:

```bash
cat > config/business_context.md <<'EOF'
- Revenue = SUM(order_items.sale_price) for items whose status is not 'Cancelled' or 'Returned'.
- "Customers" means the users table.
EOF
```

## 5. Start it

```bash
docker compose up -d
docker compose logs -f agent
```

The first start builds the agent image and downloads Open WebUI, which takes a few minutes. Wait
for the log line `curated 6/6 tables`, then press Ctrl+C.

## 6. Try it

Open `http://<VM IP>` and create an account. The first account is the admin. Then pick
**analytics-agent** and ask:

| Ask | You should see |
|---|---|
| What tables do we have? | the 6 tables with short summaries. No query runs. |
| What do the columns in order_items mean? | the columns with their types and meanings |
| How many order items are there per status? | a small table, a CSV link and "Show SQL" |
| Top 10 product categories by revenue | a join across tables that uses your revenue rule |
| Show monthly revenue for the last 12 months as a line chart | a chart with a download link |
| Now only for women's products | the same query, narrowed: a follow-up |
| List all orders from last month | the first 5 rows and a CSV link for the rest |
| Delete all cancelled orders | nothing is deleted: the agent only runs read-only queries |

If all 8 answers look like this, everything works end to end.

To check the agent itself:

```bash
docker compose exec agent python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').read().decode())"
```

It should show `"curation": "done"` and `"tables": 6`.

## 7. Clean up

The VM costs money while it runs. Delete it when you're done (in Cloud Shell):

```bash
gcloud compute instances delete analytics-agent --project "$PROJECT_ID" --zone "$ZONE"
```

To remove the demo data as well, run `DROP SCHEMA demo_shop CASCADE;` in the BigQuery console.
