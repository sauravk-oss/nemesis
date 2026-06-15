#!/bin/bash
# devtest log streamer — streams all saurav devstack pod logs to individual files
# Usage:   bash scripts/stream-logs.sh
# Stop:    pkill -f "kubectl logs.*saurav"

source ~/.devstack/shrc 2>/dev/null

LOG_DIR="/Users/saurav.k/Projects/Agents/nemesis_v2/workspace/features/enh-18651-cfb-offer-fee-fix/kubectl-logs"
mkdir -p "$LOG_DIR"

echo "🚀 Starting log streams → $LOG_DIR"
echo ""

# api
kubectl logs -n api             api-web-saurav-7578f784c7-qpfbn                        -f --timestamps=true >> "$LOG_DIR/api-web-saurav.log"                   2>&1 &
echo "  ✅ api/api-web-saurav-7578f784c7-qpfbn                      → api-web-saurav.log"

# checkout
kubectl logs -n checkout        checkout-saurav-84b949b8b4-pxbqt                       -f --timestamps=true >> "$LOG_DIR/checkout-saurav.log"                  2>&1 &
echo "  ✅ checkout/checkout-saurav-84b949b8b4-pxbqt                 → checkout-saurav.log"

# checkout-service (2 replicas — log both)
kubectl logs -n checkout-service checkout-service-saurav-84b8c96fc4-cpk66              -f --timestamps=true >> "$LOG_DIR/checkout-service-saurav-1.log"         2>&1 &
echo "  ✅ checkout-service/checkout-service-saurav-84b8c96fc4-cpk66 → checkout-service-saurav-1.log"
kubectl logs -n checkout-service checkout-service-saurav-84b8c96fc4-nsl55              -f --timestamps=true >> "$LOG_DIR/checkout-service-saurav-2.log"         2>&1 &
echo "  ✅ checkout-service/checkout-service-saurav-84b8c96fc4-nsl55 → checkout-service-saurav-2.log"

# offers-engine
kubectl logs -n offers-engine   offers-engine-live-saurav-7dbf687cb7-wn4ns             -f --timestamps=true >> "$LOG_DIR/offers-engine-live-saurav.log"         2>&1 &
echo "  ✅ offers-engine/offers-engine-live-saurav-7dbf687cb7-wn4ns  → offers-engine-live-saurav.log"
kubectl logs -n offers-engine   offers-engine-test-saurav-6776c6c9b7-n7whj             -f --timestamps=true >> "$LOG_DIR/offers-engine-test-saurav.log"         2>&1 &
echo "  ✅ offers-engine/offers-engine-test-saurav-6776c6c9b7-n7whj  → offers-engine-test-saurav.log"

# payments-card
kubectl logs -n payments-card   payments-card-live-saurav-6b49887bf-sssdm              -f --timestamps=true >> "$LOG_DIR/payments-card-live-saurav.log"         2>&1 &
echo "  ✅ payments-card/payments-card-live-saurav-6b49887bf-sssdm   → payments-card-live-saurav.log"
kubectl logs -n payments-card   payments-card-test-saurav-67f7dd958c-759x9             -f --timestamps=true >> "$LOG_DIR/payments-card-test-saurav.log"         2>&1 &
echo "  ✅ payments-card/payments-card-test-saurav-67f7dd958c-759x9  → payments-card-test-saurav.log"

# payments-nbplus
kubectl logs -n payments-nbplus payments-nbplus-live-saurav-7ff6b6d7f8-j8sd6           -f --timestamps=true >> "$LOG_DIR/payments-nbplus-live-saurav.log"       2>&1 &
echo "  ✅ payments-nbplus/payments-nbplus-live-saurav-7ff6b6d7f8    → payments-nbplus-live-saurav.log"

# pg-router
kubectl logs -n pg-router       pg-router-saurav-847bdb9b6c-bt4hz                      -f --timestamps=true >> "$LOG_DIR/pg-router-saurav.log"                  2>&1 &
echo "  ✅ pg-router/pg-router-saurav-847bdb9b6c-bt4hz               → pg-router-saurav.log"
kubectl logs -n pg-router       pg-router-worker-notification-saurav-b9468b88-jbt2d    -f --timestamps=true >> "$LOG_DIR/pg-router-worker-saurav.log"           2>&1 &
echo "  ✅ pg-router/pg-router-worker-notification-saurav-b9468b88   → pg-router-worker-saurav.log"

# splitz (now running — needed for routing experiments)
kubectl logs -n splitz          splitz-saurav-8555c64bcb-9phh5                         -f --timestamps=true >> "$LOG_DIR/splitz-saurav.log"                     2>&1 &
echo "  ✅ splitz/splitz-saurav-8555c64bcb-9phh5                     → splitz-saurav.log"

echo ""
echo "📂 Log folder: $LOG_DIR"
echo "🛑 Stop all:   pkill -f 'kubectl logs.*saurav'"
echo ""
echo "Streaming... (Ctrl+C exits this script but streams keep running in background)"

wait
