#!/usr/bin/env bash
# Creates the trade-terminal VM from OCI Cloud Shell — no console form needed.
#  - builds a VCN + public subnet + internet gateway (reuses them on re-run)
#  - for each Always Free shape (A1 first, then E2.1.Micro), tries every AD that offers it
#  - picks the newest Ubuntu 24.04 (non-Minimal) image compatible with that shape
#  - prints the public IP when done
#
# Usage (in Cloud Shell):
#   bash oci_create_vm.sh            # A1 → Micro
#   bash oci_create_vm.sh micro      # Micro only
#   bash oci_create_vm.sh a1 20      # A1 only, retry all ADs 20 rounds, 60 s apart
set -euo pipefail

MODE="${1:-auto}"
ROUNDS="${2:-1}"
C="${OCI_TENANCY:?Run this in OCI Cloud Shell (OCI_TENANCY not set)}"   # root compartment
KEY="$HOME/.ssh/oci_trade.pub"
NAME="trade-terminal"
[ -f "$KEY" ] || { echo "Missing $KEY — run: ssh-keygen -t rsa -b 4096 -f ~/.ssh/oci_trade -N \"\""; exit 1; }

case "$MODE" in
  auto)  SHAPES=("VM.Standard.A1.Flex" "VM.Standard.E2.1.Micro") ;;
  a1)    SHAPES=("VM.Standard.A1.Flex") ;;
  micro) SHAPES=("VM.Standard.E2.1.Micro") ;;
  *) echo "mode must be auto | a1 | micro"; exit 1 ;;
esac

# --- already exists? -------------------------------------------------------------
EXISTING=$(oci compute instance list -c "$C" --display-name "$NAME" --all \
  | jq -r '[.data[]? | select(."lifecycle-state" != "TERMINATED" and ."lifecycle-state" != "TERMINATING")][0].id // empty')
if [ -n "$EXISTING" ]; then
  echo "Instance '$NAME' already exists: $EXISTING"
  IP=$(oci compute instance list-vnics --instance-id "$EXISTING" | jq -r '.data[0]."public-ip" // empty')
  echo "Public IP: ${IP:-none}"
  exit 0
fi

# --- network (idempotent) ------------------------------------------------------------
echo "== Network"
VCN=$(oci network vcn list -c "$C" --display-name trade-vcn --all | jq -r '.data[0].id // empty')
if [ -z "$VCN" ]; then
  VCN=$(oci network vcn create -c "$C" --cidr-blocks '["10.0.0.0/16"]' --display-name trade-vcn \
        --dns-label tradevcn --wait-for-state AVAILABLE | jq -r '.data.id')
  echo "created VCN"
fi
IGW=$(oci network internet-gateway list -c "$C" --vcn-id "$VCN" --all | jq -r '.data[0].id // empty')
if [ -z "$IGW" ]; then
  IGW=$(oci network internet-gateway create -c "$C" --vcn-id "$VCN" --is-enabled true \
        --display-name trade-igw --wait-for-state AVAILABLE | jq -r '.data.id')
  echo "created internet gateway"
fi
RT=$(oci network vcn get --vcn-id "$VCN" | jq -r '.data."default-route-table-id"')
oci network route-table update --rt-id "$RT" --force \
  --route-rules "[{\"destination\":\"0.0.0.0/0\",\"destinationType\":\"CIDR_BLOCK\",\"networkEntityId\":\"$IGW\"}]" >/dev/null
SUBNET=$(oci network subnet list -c "$C" --vcn-id "$VCN" --display-name trade-public --all | jq -r '.data[0].id // empty')
if [ -z "$SUBNET" ]; then
  # Regional public subnet; uses the VCN default security list (SSH 22 + ICMP only).
  SUBNET=$(oci network subnet create -c "$C" --vcn-id "$VCN" --cidr-block 10.0.0.0/24 \
           --display-name trade-public --dns-label pub --wait-for-state AVAILABLE | jq -r '.data.id')
  echo "created public subnet"
fi
echo "network ready"

# --- launch ---------------------------------------------------------------------------
ADS=$(oci iam availability-domain list -c "$C" | jq -r '.data[].name')

try_launch() {  # $1 shape  $2 AD  $3 image
  local extra=()
  [ "$1" = "VM.Standard.A1.Flex" ] && extra=(--shape-config '{"ocpus":1,"memoryInGBs":6}')
  oci compute instance launch -c "$C" --availability-domain "$2" --shape "$1" "${extra[@]}" \
    --image-id "$3" --subnet-id "$SUBNET" --assign-public-ip true --display-name "$NAME" \
    --ssh-authorized-keys-file "$KEY" --wait-for-state RUNNING --max-wait-seconds 900 2>&1
}

for round in $(seq 1 "$ROUNDS"); do
  for SHAPE in "${SHAPES[@]}"; do
    IMG=$(oci compute image list -c "$C" --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
          --shape "$SHAPE" --sort-by TIMECREATED --sort-order DESC --all \
          | jq -r '[.data[] | select(."display-name" | test("Minimal") | not)][0].id // empty')
    [ -n "$IMG" ] || { echo "no Ubuntu 24.04 image for $SHAPE — skipping"; continue; }
    for AD in $ADS; do
      if ! oci compute shape list -c "$C" --availability-domain "$AD" --all \
           | jq -e --arg s "$SHAPE" '.data[] | select(.shape == $s)' >/dev/null; then
        echo "  $SHAPE not offered in $AD"
        continue
      fi
      echo "== round $round: $SHAPE in $AD"
      if OUT=$(try_launch "$SHAPE" "$AD" "$IMG"); then
        ID=$(echo "$OUT" | jq -r '.data.id' 2>/dev/null || true)
        IP=$(oci compute instance list-vnics --instance-id "$ID" | jq -r '.data[0]."public-ip"')
        echo
        echo "SUCCESS: $SHAPE in $AD"
        echo "Public IP: $IP"
        echo "Next:  scp -i ~/.ssh/oci_trade trade-terminal.zip ubuntu@$IP:~"
        echo "       ssh -i ~/.ssh/oci_trade ubuntu@$IP"
        exit 0
      fi
      echo "$OUT" | grep -oiE 'out of (host )?capacity|LimitExceeded[^"]*|"message": "[^"]*"' | head -1 | sed 's/^/  failed: /' || true
    done
  done
  if [ "$round" -lt "$ROUNDS" ]; then echo "retrying in 60 s..."; sleep 60; fi
done
echo
echo "No capacity on any tried shape/AD. Options: bash $0 a1 30   (keep retrying A1), or upgrade to Pay As You Go."
exit 1
