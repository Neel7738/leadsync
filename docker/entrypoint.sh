#!/bin/bash
set -e

# ════════════════════════════════════════════════════════════════
# AI Sales Follow-Up Agent — Container Entrypoint
# ════════════════════════════════════════════════════════════════
# Usage:
#   docker run ... api           → Start FastAPI only (default)
#   docker run ... dashboard     → Start Streamlit only
#   docker run ... all           → Start both API + Streamlit
#   docker run ... worker        → Start API + SLA checker background task

MODE=${1:-api}
WORKERS=${WORKERS:-2}
HOST=${HOST:-0.0.0.0}
API_PORT=${PORT:-8000}
DASHBOARD_PORT=${DASHBOARD_PORT:-8501}

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

start_api() {
    log "Starting FastAPI on ${HOST}:${API_PORT} (workers=${WORKERS})..."
    uvicorn api.app:app \
        --host "$HOST" \
        --port "$API_PORT" \
        --workers "$WORKERS" \
        --log-level info \
        --access-log &
    API_PID=$!
    log "API started (PID: ${API_PID})"
}

start_dashboard() {
    log "Starting Streamlit dashboard on ${HOST}:${DASHBOARD_PORT}..."
    streamlit run ui/streamlit/app.py \
        --server.address "$HOST" \
        --server.port "$DASHBOARD_PORT" \
        --server.headless true \
        --server.fileWatcherType none &
    DASHBOARD_PID=$!
    log "Dashboard started (PID: ${DASHBOARD_PID})"
}

# Wait for any process to exit
wait_for_exit() {
    trap "log 'Shutting down...'; kill $(jobs -p) 2>/dev/null; exit 0" SIGTERM SIGINT
    wait -n 2>/dev/null || true
}

case "$MODE" in
    api)
        start_api
        wait_for_exit
        ;;
    dashboard)
        start_dashboard
        wait_for_exit
        ;;
    all)
        start_api
        start_dashboard
        wait_for_exit
        ;;
    worker)
        start_api
        log "SLA checker would run as background task (implement via cron or celery)"
        wait_for_exit
        ;;
    test)
        log "Running tests..."
        python -m pytest tests/ -v --tb=short
        ;;
    *)
        log "Unknown mode: $MODE"
        log "Usage: entrypoint.sh [api|dashboard|all|worker|test]"
        exit 1
        ;;
esac
