# AGENTS.md — Operation Shadow Development Guidelines

## 1. Project Overview & Source of Truth
Operation Shadow is a local, indoor multi-vehicle (`SCOUT01` ground rover and `HAWK01` aerial drone) mission-control system.
- **Primary Architecture Specification**: `docs/Operation Shadow — Backend & Frontend Architecture.md`
- **Implementation Plan & Milestones**: `docs/implementation-plan.md`
- **Assumptions & Unresolved External Specs**: `docs/assumptions.md`
- **UI & Tactical Design Specification**: `docs/ui-design-spec.md`

## 2. Non-Negotiable Architectural & Safety Invariants
1. **No Unconfirmed Hardware Control**: Real hardware (`mode = "real"`) must **never** be activated or controlled until external specifications (**TRD v2.0**, **Hardware & IoT**, **ULTRON Agent Architecture**) and physical failsafes are confirmed. `SHADOW_HARDWARE_LOCKOUT=true` and `simulated` mode are mandatory defaults.
2. **Do Not Invent Missing External Contracts**: Do not fabricate proprietary vehicle protocols, telemetry binary/JSON layouts, command schemas (TRD v2.0 §7), or ULTRON planning logic. Keep all provisional contracts isolated behind explicitly labeled stub interfaces in `backend/ultron_stub/` and documented in `docs/assumptions.md`.
3. **In-Process ULTRON Execution**: The ULTRON agent module runs inside the FastAPI process (`backend/fastapi/`) via direct Python imports—never as a separate networked microservice.
4. **Emergency Stop Fast Path (§10)**: `POST /emergency-stop` must dispatch the `STOP` command directly through `vehicle_gateway.py` without passing through ULTRON validation, task queues, or synchronous SQLite writes. Database logging must occur asynchronously *after* dispatch.
5. **Direct Camera Feeds (§8.3)**: Camera MJPEG streams are fetched directly by the React dashboard from the vehicles (or simulated placeholder in simulated mode) and must never be proxied or relayed through the FastAPI backend.
6. **Push-Only Dashboard Updates (§4, §5.2)**: Routine updates (`telemetry`, `event`, `reasoning_trace`, `mission_status`) are pushed over the dashboard WebSocket (`dashboard_gateway.py`), with a full-state snapshot sent immediately upon connection. The frontend must never poll REST endpoints for routine updates and must never compute derived mission state locally.

## 3. Persistent Visual Design Rules (Tactical Command-and-Control Aesthetic)
Operation Shadow must have a professional military tactical command-and-control aesthetic, not a generic AI/SaaS dashboard. All frontend components and layouts must adhere to `docs/ui-design-spec.md` and the following rules:
1. **Surface Palette**: Use dark charcoal (`#0B0E0D` to `#141917`) and near-black backgrounds with high-contrast, low-glare matte surfaces suitable for low-light operational environments.
2. **Tactical Accents**: Use muted olive-drab (`#4A5D3F`), military green (`#5C744D`), and desaturated sage (`#8AA37B`) accents for active states, tactical overlays, and operational indicators.
3. **Typography & Data Readouts**: Use warm off-white primary text (`#E6E8E1`) paired with strict monospaced typography (`tabular-nums`) for all telemetry values, `x/y/alt` coordinates, battery levels, timestamps, mission IDs, and vehicle identifiers (`SCOUT01`, `HAWK01`).
4. **Command-Console Layout & Framing**: Build compact operational panels with sharp or minimally radiused corners (`0px–2px`), thin hair-line borders (`1px solid`), subtle coordinate grid lines, and a dense, high-signal desktop command-console layout that avoids unnecessary page scrolling.
5. **Alert Hierarchy & Emergency Stop**: Use high-visibility amber (`#D98A2B`) for warnings, degraded links, and `UNKNOWN` heartbeat states, and reserve a clearly distinguishable, unmistakable crimson/red control (`#C93535` / `#E03E3E`) exclusively for the Emergency Stop (`POST /emergency-stop`) control and critical faults.
6. **Prohibited Visual Patterns**: Strictly avoid purple/indigo gradients, cyberpunk neon effects, excessive glassmorphism/blur, oversized rounded SaaS cards, decorative AI sparkles/avatars/imagery, and generic consumer dashboard templates.

## 4. Tech Stack & Conventions
- **Backend**: Python 3.13+, FastAPI, SQLAlchemy 2.0 (async) with `aiosqlite`, Pydantic v2 / `pydantic-settings`.
- **Frontend**: Vite, React, TypeScript, React Context + `useReducer` for state management (no Redux, no Leaflet/Mapbox).
- **Important Python Import Note**: The backend application lives in `backend/fastapi/` per §3 of the architecture specification. Never place an `__init__.py` at `backend/fastapi/__init__.py` so that `backend/fastapi` does not shadow the installed `fastapi` PyPI library. Always run backend commands with `PYTHONPATH=fastapi:.` from the `backend/` directory (configured automatically for `pytest` and `mypy` in `backend/pyproject.toml`).

## 5. Verification Commands
Before completing any milestone, run and verify all checks:

### Backend Checks
```bash
cd backend
.venv/bin/pytest
.venv/bin/mypy fastapi ultron_stub tests
```

### Frontend Checks
```bash
cd dashboard/react
npm run typecheck
npm test -- --run
npm run build
```
