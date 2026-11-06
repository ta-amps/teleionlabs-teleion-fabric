#!/usr/bin/env bash
set -euo pipefail

# Modular Cloudflare edge deployment helper for Tessera + Herma + Agora Pages.
# Run with DRY_RUN=1 (default) to preview commands, or DRY_RUN=0 to execute via Wrangler.

DRY_RUN="${DRY_RUN:-1}"
TESSERA_DIR="${TESSERA_DIR:-../tessera-infrastructure-work-and-storage-engine}"
HERMA_DIR="${HERMA_DIR:-../herma-infrastructure-shim-for-intelligent-agents}"
AGORA_DIR="${AGORA_DIR:-../agora-public-overview-architecture-setup-and-contribution}"

run_cmd() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] $*"
  else
    echo "[exec] $*"
    "$@"
  fi
}

echo "=== Step 1: Deploy the Teleion Fabric work and storage engine ==="
run_cmd npx --prefix "$TESSERA_DIR" wrangler deploy

echo "=== Step 2: Deploy the Teleion Fabric model shim ==="
run_cmd npx --prefix "$HERMA_DIR" wrangler deploy

echo "=== Step 3: Deploy Agora Static Hub to Cloudflare Pages ==="
run_cmd npx wrangler pages deploy "$AGORA_DIR/site" --project-name=agora-hub
