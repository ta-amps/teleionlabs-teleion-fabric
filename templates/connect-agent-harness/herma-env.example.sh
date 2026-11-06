#!/usr/bin/env bash
# Example environment setup for connecting Muse Code, OpenCode or Pi through the Teleion Fabric model shim.
# The HERMA_* variable names are kept for existing scripts.
# Source in your shell after substituting your own local or Standard-tier endpoint.
# Defaults: the Meta API (Standard tier) and Muse Spark 1.3. Keep the API key in your shell keychain, not here.

export HERMA_BASE_URL="${HERMA_BASE_URL:-https://api.meta.ai/v1}"
export HERMA_HARNESS="${HERMA_HARNESS:-opencode}"
export HERMA_MODEL="${HERMA_MODEL:-muse-spark-1.3}"
export HERMA_TIER_POLICY="standard_or_local"

# Local developer sandbox endpoint (used by python3 -m sandbox.worker_smoke)
export TESSERA_SANDBOX_URL="${TESSERA_SANDBOX_URL:-http://127.0.0.1:8787}"

echo "Configured harness=${HERMA_HARNESS} model=${HERMA_MODEL} policy=${HERMA_TIER_POLICY}"
