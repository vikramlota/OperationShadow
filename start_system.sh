#!/bin/bash
# =============================================================================
# Operation Shadow — Single-command system launcher
# Usage: ./start_system.sh
# =============================================================================

set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
UVICORN="${UVICORN:-uvicorn}"

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; AMBER='\033[0;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[SHADOW]${NC} $*"; }
warn()  { echo -e "${AMBER}[SHADOW]${NC} $*"; }
error() { echo -e "${RED}[SHADOW]${NC} $*"; }

echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║    OPERATION SHADOW — SYSTEM LAUNCH      ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""

# ── Pre-flight checks ─────────────────────────────────────────────────────────
info "Running pre-flight checks..."

if ! command -v "$PYTHON" >/dev/null 2>&1 && [ ! -f "$PYTHON" ]; then
    error "Python not found at $PYTHON. Set PYTHON or activate your virtualenv."
    exit 1
fi

if ! command -v "$UVICORN" >/dev/null 2>&1 && [ ! -f "$UVICORN" ]; then
    error "uvicorn not found at $UVICORN. Run: pip install uvicorn or set UVICORN."
    exit 1
fi

if [ ! -f "${PROJECT_ROOT}/frontend/node_modules/.bin/vite" ]; then
    warn "node_modules missing. Running npm install..."
    cd "${PROJECT_ROOT}/frontend" && npm install
    npm install-scripts approve esbuild 2>/dev/null || true
fi

if [ ! -f "${PROJECT_ROOT}/.env" ]; then
    warn ".env not found — backend will use defaults (fallback LLM cache)."
fi

info "Pre-flight OK."
echo ""

# ── Cleanup on exit ───────────────────────────────────────────────────────────
BACKEND_PID=""
FRONTEND_PID=""

cleanup() {
    echo ""
    warn "Shutting down Operation Shadow..."
    [ -n "$BACKEND_PID" ]  && kill "$BACKEND_PID"  2>/dev/null && info "Backend stopped."
    [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null && info "Dashboard stopped."
    info "System offline. Out."
    exit 0
}
trap cleanup INT TERM

# ── 1. FastAPI C2 Backend ─────────────────────────────────────────────────────
info "[1/2] Starting FastAPI C2 Backend..."
cd "${PROJECT_ROOT}/backend"
PYTHONPATH=fastapi:.:.. \
    "$UVICORN" main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --reload \
    --log-level warning \
    2>&1 | sed "s/^/  [BACKEND] /" &
BACKEND_PID=$!

# Wait for backend to be ready
info "Waiting for backend to come online..."
for i in $(seq 1 20); do
    if curl -sf http://127.0.0.1:8000/health > /dev/null 2>&1; then
        info "Backend is ONLINE ✓"
        break
    fi
    sleep 0.5
    if [ $i -eq 20 ]; then
        warn "Backend health check timed out — it may still be starting."
    fi
done

# ── 2. React Tactical Dashboard ───────────────────────────────────────────────
info "[2/2] Starting React Tactical Dashboard..."
cd "${PROJECT_ROOT}/frontend"
npm run dev 2>&1 | sed "s/^/  [FRONTEND] /" &
FRONTEND_PID=$!

# ── Ready ─────────────────────────────────────────────────────────────────────
sleep 1
echo ""
echo "  ┌──────────────────────────────────────────┐"
echo "  │         OPERATION SHADOW ONLINE          │"
echo "  │                                          │"
echo "  │  Backend  → http://localhost:8000        │"
echo "  │  API Docs → http://localhost:8000/docs   │"
echo "  │  Health   → http://localhost:8000/health │"
echo "  │  Dashboard→ http://localhost:5173        │"
echo "  │                                          │"
echo "  │  Press Ctrl+C to shut down.              │"
echo "  └──────────────────────────────────────────┘"
echo ""

wait
