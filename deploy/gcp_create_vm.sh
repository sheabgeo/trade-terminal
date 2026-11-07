#!/usr/bin/env bash
# Creates the trade-terminal VM on Google Cloud. Run in Google Cloud Shell (>_ icon in the console).
#   bash gcp_create_vm.sh              # e2-small (2 vCPU shared, 2 GB) ≈ $13/mo incl. disk → paid by the $300 credit
#   bash gcp_create_vm.sh e2-medium    # 4 GB, ≈ $27/mo — if Claude Code feels slow
set -euo pipefail
MACHINE="${1:-e2-small}"
NAME="${NAME:-trade-terminal}"
ZONE="${ZONE:-us-central1-a}"     # Iowa: closest cheap region to Texas
PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
[ -n "$PROJECT" ] || { echo "No project selected. Run: gcloud projects list   then: gcloud config set project <PROJECT_ID>"; exit 1; }
echo "Project: $PROJECT   Zone: $ZONE   Machine: $MACHINE"

echo "== Enabling Compute Engine API (first time can take ~1 min)"
gcloud services enable compute.googleapis.com

if gcloud compute instances describe "$NAME" --zone "$ZONE" >/dev/null 2>&1; then
  echo "VM '$NAME' already exists — not creating another."
else
  # Filter locally: the server-side filter language rejects regex + NOT combinations.
  FAMILY="$(gcloud compute images list --project ubuntu-os-cloud --no-standard-images \
            --format="value(family,architecture)" \
            | awk '$1 ~ /^ubuntu-2404/ && $1 !~ /minimal|arm64|pro/ && $2 == "X86_64" {print $1}' | sort -u | head -1)"
  [ -n "$FAMILY" ] || { echo "Could not find an Ubuntu 24.04 x86 image family"; exit 1; }
  echo "== Creating $NAME from image family $FAMILY"
  gcloud compute instances create "$NAME" --zone "$ZONE" --machine-type "$MACHINE" \
    --image-family "$FAMILY" --image-project ubuntu-os-cloud \
    --boot-disk-size 30GB --boot-disk-type pd-balanced \
    --labels app=trade-terminal
fi

IP="$(gcloud compute instances describe "$NAME" --zone "$ZONE" --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"
echo
echo "VM ready. External IP: ${IP:-none}"
echo "Next (in Cloud Shell, after uploading trade-terminal.zip via the ⋮ menu → Upload):"
echo "  gcloud compute scp ~/trade-terminal.zip $NAME:~ --zone $ZONE"
echo "  gcloud compute ssh $NAME --zone $ZONE"
