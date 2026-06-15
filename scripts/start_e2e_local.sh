#!/bin/bash
# start_e2e_local.sh — Local E2E environment launcher for Nemesis v2
# Usage: ./scripts/start_e2e_local.sh [service|roast|orchestrator|status]

set -e
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPOS="$REPO_ROOT/workspace/repos"

# ── Color helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*"; }
info() { echo -e "${BLUE}→${NC} $*"; }

CMD="${1:-status}"

# ── Status ────────────────────────────────────────────────────────────────────
show_status() {
  echo ""
  echo "════════════════════════════════════════════════════"
  echo "  Nemesis E2E — Local Environment Status"
  echo "════════════════════════════════════════════════════"
  echo ""

  # Docker
  if docker info &>/dev/null; then
    ok "Docker running"
    if docker images roast:master --quiet 2>/dev/null | grep -q .; then
      ok "ROAST image (roast:master) available"
    else
      warn "ROAST image not built — run: ./scripts/start_e2e_local.sh build-roast"
    fi
  else
    err "Docker not running — start Docker Desktop"
  fi

  # Go toolchain
  if command -v go &>/dev/null; then
    ok "Go $(go version | awk '{print $3}') available"
  else
    warn "Go not found — e2e dir tests require Go"
  fi

  # e2e-test-orchestrator
  if curl -sf http://localhost:8080/healthz &>/dev/null || \
     curl -sf http://localhost:9400/healthz &>/dev/null; then
    ok "e2e-test-orchestrator running"
  else
    warn "e2e-test-orchestrator not running — run: ./scripts/start_e2e_local.sh orchestrator"
  fi

  # Services with e2e/ dirs
  echo ""
  echo "Services with local e2e/ tests:"
  for slug in offers-engine emandate-service checkout-service payments-card payments-upi pg-router api shield dcs subscriptions stork; do
    if [ -d "$REPOS/$slug/e2e" ]; then
      count=$(find "$REPOS/$slug/e2e" -name "*_test.go" | wc -l | tr -d ' ')
      echo "  ✓ $slug ($count test files)"
    else
      echo "  — $slug (no e2e/ dir — uses ROAST)"
    fi
  done

  echo ""
  echo "ROAST groups available:"
  echo "  PAYMENT, CARD, UPI, EMANDATE, MANDATE, OFFER, SUBSCRIPTION, REFUND, SETTLEMENT"
  echo ""
}

# ── Build ROAST ───────────────────────────────────────────────────────────────
build_roast() {
  info "Building ROAST docker image..."
  cd "$REPOS/roast"
  docker build . -t roast:master
  ok "ROAST image built: roast:master"
}

# ── Start e2e-test-orchestrator ───────────────────────────────────────────────
start_orchestrator() {
  info "Starting e2e-test-orchestrator with Docker Compose..."
  cd "$REPOS/e2e-test-orchestrator"

  # Need GIT_TOKEN for private deps
  if [ -z "$GIT_TOKEN" ]; then
    warn "GIT_TOKEN not set — private Go modules may fail"
    warn "Export it: export GIT_TOKEN=\$(gh auth token)"
  fi

  export GIT_USERNAME="${GIT_USERNAME:-$(gh api user --jq .login 2>/dev/null || echo 'git')}"
  export GIT_TOKEN="${GIT_TOKEN:-$(gh auth token 2>/dev/null || echo '')}"

  docker-compose -f deployment/dev/docker-compose.yml up -d db redis
  info "Waiting for MySQL to be ready..."
  sleep 5

  docker-compose -f deployment/dev/docker-compose.yml up -d migration
  sleep 3

  docker-compose -f deployment/dev/docker-compose.yml up -d api
  ok "e2e-test-orchestrator started at http://localhost:9400"
}

# ── Run single service e2e tests (go test) ───────────────────────────────────
run_service() {
  SERVICE="$2"
  ENV="${3:-e2e}"
  DEVSTACK_LABEL="${4:-}"

  if [ -z "$SERVICE" ]; then
    err "Usage: $0 service <slug> [env] [devstack_label]"
    echo "  Examples:"
    echo "    $0 service offers-engine e2e saurav.k"
    echo "    $0 service checkout-service e2e"
    exit 1
  fi

  REPO="$REPOS/$SERVICE"
  if [ ! -d "$REPO/e2e" ]; then
    warn "$SERVICE has no e2e/ directory"
    info "Falling back to ROAST..."
    run_roast "$SERVICE"
    return
  fi

  info "Running e2e tests for $SERVICE (APP_ENV=$ENV)..."
  cd "$REPO"

  export APP_ENV="$ENV"
  [ -n "$DEVSTACK_LABEL" ] && export DEVSTACK_LABEL="$DEVSTACK_LABEL"

  go test ./e2e/... -v -timeout 300s 2>&1
}

# ── Run ROAST for a service ───────────────────────────────────────────────────
run_roast() {
  SERVICE="${2:-}"
  ENV="${3:-test}"
  MODE="${4:-intg}"

  # Service → ROAST group mapping
  declare -A GROUPS=(
    ["emandate-service"]="EMANDATE,MANDATE,RECURRING"
    ["payments-mandate"]="MANDATE,EMANDATE"
    ["offers-engine"]="OFFER,OFFERS,INSTANT_DISCOUNT"
    ["payments-card"]="PAYMENT,CARD,CARDPAYMENT"
    ["payments-upi"]="PAYMENT,UPI"
    ["pg-router"]="PAYMENT"
    ["checkout-service"]="CHECKOUT"
    ["subscriptions"]="SUBSCRIPTION"
    ["settlements"]="SETTLEMENT,SCROOGE"
    ["scrooge"]="SETTLEMENT,SCROOGE"
  )

  INCLUDE_GROUPS="${GROUPS[$SERVICE]:-PAYMENT}"

  if ! docker images roast:master --quiet 2>/dev/null | grep -q .; then
    warn "ROAST image not found. Building..."
    build_roast
  fi

  info "Running ROAST for $SERVICE (groups=$INCLUDE_GROUPS, ENV=$ENV)..."
  docker run --rm \
    -e ENV="$ENV" \
    -e MODE="$MODE" \
    -e INCLUDE_GROUPS="$INCLUDE_GROUPS" \
    roast:master
}

# ── Run full service pipeline ─────────────────────────────────────────────────
run_pipeline() {
  SLUG="$2"
  SERVICES="${3:-}"  # comma-separated, e.g. "offers-engine,emandate-service"
  ENV="${4:-e2e}"

  if [ -z "$SLUG" ]; then
    err "Usage: $0 pipeline <feature-slug> <service1,service2,...> [env]"
    exit 1
  fi

  if [ -z "$SERVICES" ]; then
    err "Provide comma-separated services: $0 pipeline dfb-fix offers-engine,emandate-service"
    exit 1
  fi

  info "Running service pipeline for feature: $SLUG"
  info "Services: $SERVICES"
  info "Using Nemesis /e2e skill via Python..."

  python3 -c "
import sys, json
sys.path.insert(0, '$REPO_ROOT')
from scripts.rubick_e2e import run_service_pipeline, service_pipeline_report
from pathlib import Path

slug = '$SLUG'
services = '$SERVICES'.split(',')
env = '$ENV'

print(f'Running {len(services)} service(s) in parallel...')
result = run_service_pipeline(slug, services, env=env)

report = service_pipeline_report(slug, result)
report_path = Path('workspace/features') / slug / 'e2e-report.md'
report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text(report)

print(report)
print()
print(f'Report saved: {report_path}')
print(f'Overall: {result[\"overall_status\"]}')
"
}

# ── Main ──────────────────────────────────────────────────────────────────────
case "$CMD" in
  status)         show_status ;;
  build-roast)    build_roast ;;
  orchestrator)   start_orchestrator ;;
  service)        run_service "$@" ;;
  roast)          run_roast "$@" ;;
  pipeline)       run_pipeline "$@" ;;
  *)
    echo "Usage: $0 [status|build-roast|orchestrator|service|roast|pipeline]"
    echo ""
    echo "  status                           — Show local E2E environment status"
    echo "  build-roast                      — Build ROAST docker image"
    echo "  orchestrator                     — Start e2e-test-orchestrator locally"
    echo "  service <slug> [env] [label]     — Run go test ./e2e/... for a service"
    echo "  roast <slug> [env] [mode]        — Run ROAST docker for a service"
    echo "  pipeline <slug> <svcs> [env]     — Run all services in parallel"
    echo ""
    echo "Examples:"
    echo "  $0 status"
    echo "  $0 service offers-engine e2e saurav.k"
    echo "  $0 roast emandate-service test intg"
    echo "  $0 pipeline dfb-fix offers-engine,emandate-service e2e"
    ;;
esac
