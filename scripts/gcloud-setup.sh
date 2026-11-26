#!/usr/bin/env bash
# One-shot Google Cloud setup for the analytics agent. Safe to run again.
#
# Run it in Cloud Shell (already signed in) or anywhere after `gcloud auth login`:
#   bash scripts/gcloud-setup.sh YOUR_PROJECT_ID [ZONE]
#
# It does steps 1-3 of docs/deploy-vm.md: enables the APIs, creates the service account
# (BigQuery read + run queries), reserves a static IP, opens ports 80/443 and creates the VM
# with Docker, Compose and Git already installed. Then it prints what to do next.

set -euo pipefail

PROJECT_ID="${1:-${PROJECT_ID:-}}"
ZONE="${2:-${ZONE:-asia-south1-a}}"
if [[ -z "$PROJECT_ID" ]]; then
  echo "Usage: bash scripts/gcloud-setup.sh PROJECT_ID [ZONE]" >&2
  exit 1
fi
REGION="${ZONE%-*}"
SA_NAME="analytics-agent"
SA="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
VM="analytics-agent"
IP_NAME="analytics-agent-ip"

echo "==> Project: $PROJECT_ID   Zone: $ZONE"
gcloud config set project "$PROJECT_ID" >/dev/null

echo "==> Enabling APIs (can take a minute)"
gcloud services enable compute.googleapis.com bigquery.googleapis.com \
  iam.googleapis.com --project "$PROJECT_ID"

echo "==> Service account"
if ! gcloud iam service-accounts describe "$SA" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT_ID" \
    --display-name "Analytics agent"
  sleep 10  # a new service account needs a moment before it can get roles
fi
for role in roles/bigquery.dataViewer roles/bigquery.jobUser; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:$SA" --role "$role" --condition=None >/dev/null
done

echo "==> Static IP"
if ! gcloud compute addresses describe "$IP_NAME" --project "$PROJECT_ID" \
    --region "$REGION" >/dev/null 2>&1; then
  gcloud compute addresses create "$IP_NAME" --project "$PROJECT_ID" --region "$REGION"
fi
IP="$(gcloud compute addresses describe "$IP_NAME" --project "$PROJECT_ID" \
  --region "$REGION" --format 'value(address)')"

echo "==> Firewall (ports 80 and 443)"
if ! gcloud compute firewall-rules describe analytics-agent-web \
    --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud compute firewall-rules create analytics-agent-web --project "$PROJECT_ID" \
    --network default --target-tags analytics-agent \
    --allow tcp:80,tcp:443,udp:443 --source-ranges 0.0.0.0/0
fi

# Runs once on the VM's first boot: installs Docker Engine, the Compose plugin and Git.
STARTUP="$(mktemp)"
cat >"$STARTUP" <<'EOF'
#!/usr/bin/env bash
set -eux
[ -f /var/lib/analytics-agent-ready ] && exit 0
apt-get update
apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat >/etc/apt/sources.list.d/docker.sources <<EOT
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOT
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
  docker-compose-plugin
touch /var/lib/analytics-agent-ready
EOF

echo "==> VM"
if ! gcloud compute instances describe "$VM" --project "$PROJECT_ID" --zone "$ZONE" \
    >/dev/null 2>&1; then
  gcloud compute instances create "$VM" \
    --project "$PROJECT_ID" --zone "$ZONE" \
    --machine-type e2-medium \
    --image-family debian-13 --image-project debian-cloud \
    --boot-disk-size 30GB \
    --address "$IP_NAME" \
    --tags analytics-agent \
    --service-account "$SA" --scopes cloud-platform \
    --metadata-from-file startup-script="$STARTUP"
else
  echo "VM '$VM' already exists, leaving it as it is."
fi
rm -f "$STARTUP"

DASHED="${IP//./-}"
cat <<EOF

Done. Your VM's IP address is: $IP

Next:
 1. Wait about 3 minutes for Docker to finish installing on the VM.
 2. Domain: point an A record at $IP, or for a quick demo use  $DASHED.sslip.io
    (a free name that points at that IP). Use it as DOMAIN in .env.
 3. Connect:  gcloud compute ssh $VM --project $PROJECT_ID --zone $ZONE
    On the VM, check:  docker compose version
 4. Continue from step 5 of docs/deploy-vm.md (Google login), then step 7 (code and .env).
EOF
