#!/usr/bin/env bash
# Start, stop or check the analytics-agent VM. For Cloud Shell, Linux and macOS.
# On Windows, double-click scripts\vm-start.cmd, vm-stop.cmd or vm-status.cmd instead.
#
#   bash scripts/vm.sh start|stop|status [PROJECT_ID] [ZONE]
#
# PROJECT_ID defaults to the project from `gcloud config set project`. ZONE defaults to
# asia-south1-a, like scripts/gcloud-setup.sh.

set -uo pipefail

ACTION="${1:-}"
PROJECT_ID="${2:-${PROJECT_ID:-}}"
ZONE="${3:-${ZONE:-asia-south1-a}}"
VM="analytics-agent"
# If an API is off, fail with a clear message instead of a yes/no question.
export CLOUDSDK_CORE_SHOULD_PROMPT_TO_ENABLE_API=false
ERR="$(mktemp)"
trap 'rm -f "$ERR"' EXIT

die() {
  echo "ERROR: $*" >&2
  exit 1
}

# Runs gcloud without questions. Its error text goes to the file $ERR.
gcloud_q() { gcloud "$@" --quiet 2>"$ERR"; }

# Turns a gcloud error into a short message that says what to do.
explain_error() {
  local text
  text="$(cat "$ERR")"
  if grep -qiE 'auth login|auth tokens|active account' <<<"$text"; then
    die "You are not signed in to Google Cloud. Run: gcloud auth login"
  elif grep -qi 'billing' <<<"$text"; then
    die "Billing is off for project $PROJECT_ID. Turn it on in the Cloud Console (Billing)."
  elif grep -qiE 'has not been used|SERVICE_DISABLED' <<<"$text"; then
    die "The Compute Engine API is off in $PROJECT_ID, so there is no VM yet." \
      "Create it first with scripts/gcloud-setup.sh."
  elif grep -qi 'permission' <<<"$text"; then
    die "Your account may not manage VMs in $PROJECT_ID. Sign in as the project owner" \
      "(gcloud auth login), or ask the owner for the role 'Compute Instance Admin (v1)'."
  elif grep -qiE 'was not found|notFound' <<<"$text"; then
    die "There is no VM named $VM in zone $ZONE of $PROJECT_ID. Create it with" \
      "scripts/gcloud-setup.sh. To list your VMs: gcloud compute instances list"
  fi
  die "gcloud said: $text"
}

# Prints the VM's state and IP address, for example "RUNNING  34.100.1.2".
vm_info() {
  gcloud_q compute instances describe "$VM" --project "$PROJECT_ID" --zone "$ZONE" \
    --format 'value(status,networkInterfaces[0].accessConfigs[0].natIP)'
}

case "$ACTION" in
  start | stop | status) ;;
  *) die "Usage: bash scripts/vm.sh start|stop|status [PROJECT_ID] [ZONE]" ;;
esac
command -v gcloud >/dev/null || die "gcloud is not installed: https://cloud.google.com/sdk/docs/install"
[[ -n "$PROJECT_ID" ]] || PROJECT_ID="$(gcloud config get-value project 2>/dev/null)"
[[ -n "$PROJECT_ID" ]] || die "No project is set. Run: gcloud config set project YOUR_PROJECT_ID"

# Read the VM's state. If the sign-in is missing or expired, sign in and try once more.
if ! INFO="$(vm_info)"; then
  if grep -qiE 'auth login|auth tokens|active account' "$ERR"; then
    echo "Your Google Cloud sign-in is missing or expired. Signing in..."
    gcloud auth login
    INFO="$(vm_info)" || explain_error
  else
    explain_error
  fi
fi
read -r STATE IP <<<"$INFO"
case "$STATE" in
  RUNNING) WORD="running" ;;
  TERMINATED) WORD="stopped" ;;
  *) WORD="busy" ;;
esac
echo "VM $VM in project $PROJECT_ID (zone $ZONE): $STATE ($WORD)"

case "$ACTION:$STATE" in
  start:RUNNING) echo "It is already running." ;;
  start:TERMINATED)
    echo "Starting the VM. This takes about 30 seconds..."
    gcloud_q compute instances start "$VM" --project "$PROJECT_ID" --zone "$ZONE" >/dev/null ||
      explain_error
    echo "Started. The app starts by itself in 1 to 2 minutes."
    echo "It costs money while it runs. Stop it with: bash scripts/vm.sh stop"
    ;;
  stop:TERMINATED) echo "It is already stopped." ;;
  stop:RUNNING)
    echo "Stopping the VM. This can take up to 2 minutes..."
    gcloud_q compute instances stop "$VM" --project "$PROJECT_ID" --zone "$ZONE" >/dev/null ||
      explain_error
    echo "Stopped. The website is off until you start the VM again."
    echo "While stopped, you still pay a little for the disk and the IP address."
    ;;
  status:*) ;;
  *) die "The VM is busy ($STATE). Wait a minute, then try again." ;;
esac

if [[ "$ACTION" != stop && -n "${IP:-}" ]]; then
  echo "IP address: $IP (it stays the same after a restart)"
  echo "With sslip.io, the website is: https://${IP//./-}.sslip.io"
fi
