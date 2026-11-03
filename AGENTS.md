# AGENTS.md — Operation Shadow Development Guidelines

## 1. Project Overview & Source of Truth
Operation Shadow is a local robotics mission-control and environmental intelligence platform powered by **ULTRON**, coordinating the **SCOUT01** ground rover through a FastAPI backend, read-only ROS 2 perception integration, and a real-time React operations dashboard. The **HAWK01** aerial drone from the original v2.0 concept remains **simulated / roadmap, not integrated**.

- **Canonical Product Language & Terminology**: `docs/PRODUCT_LANGUAGE.md`
- **Primary Architecture Specification**: `docs/Operation Shadow — Backend & Frontend Architecture.md`
- **Implementation Plan & Milestones**: `docs/implementation-plan.md`
- **Assumptions & Implementation Status**: `docs/assumptions.md`
- **UI & Operations Console Design Specification**: `docs/ui-design-spec.md`

## 2. Non-Negotiable Architectural, Epistemic & Safety Invariants
1. **No Unconfirmed Hardware Control**: Real hardware (`mode = "real"`) must **never** be activated or controlled until external specifications (**TRD v2.0**, **Hardware & IoT**, **ULTRON Agent Architecture**) and physical failsafes are confirmed by a human. `SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_DEFAULT_VEHICLE_MODE=simulated`, `SHADOW_SCOUT_SERIAL_ENABLED=false`, and `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=false` are mandatory defaults.
2. **SCOUT01 Active Vehicle Scope**: Keep `SCOUT01` as the sole active integrated vehicle in ULTRON, the FastAPI vehicle service, and the React operations dashboard unless the architecture is explicitly changed. Always label `HAWK01` as **simulated / roadmap, not integrated**; do not delete existing `HAWK01` simulation stubs or `hawk_geofence` schema compatibility fields.
3. **Strict Provenance & Epistemic Honesty**: Clearly distinguish simulated outcomes (`SIMULATED_COMPLETED`), planned/assigned tasks (`ASSIGNED`), and open-loop serial dispatch (`COMMAND_DISPATCHED`) from verified physical execution. Never project camera-relative detections (`oak_rgb_camera_optical_frame`, `oak_parent_frame`) onto the global floor map unless `SHADOW_ROS_MAP_FRAME_CONFIRMED=true` is calibrated and verified.
4. **Do Not Invent Missing External Contracts**: Do not fabricate proprietary vehicle protocols, telemetry binary/JSON layouts, command schemas (TRD v2.0 §7), or unverified sensor streams. Keep all provisional contracts isolated behind explicitly labeled stub interfaces in `backend/ultron_stub/` and documented in `docs/assumptions.md`.
5. **In-Process ULTRON Execution**: The ULTRON agent module runs inside the FastAPI process (`backend/fastapi/`) via direct Python imports—never as a separate networked microservice.
6. **Emergency Stop Fast Path (§10)**: `POST /emergency-stop` must dispatch the `STOP` command directly through `vehicle_gateway.py` without passing through ULTRON validation, task queues, or synchronous SQLite writes. Database logging must occur asynchronously *after* dispatch.
7. **Direct Camera Feeds (§8.3)**: Camera MJPEG and direct browser ROS streams are fetched directly by the React dashboard from `SCOUT01` (or simulated/standby state) and must never be proxied or relayed through the FastAPI backend.
8. **Push-Only Dashboard Updates (§4, §5.2)**: Routine updates (`telemetry`, `detections`, `event`, `reasoning_trace`, `mission_status`) are pushed over the dashboard WebSocket (`dashboard_gateway.py`), with a full-state snapshot sent immediately upon connection. The frontend must never poll REST endpoints for routine updates and must never compute derived mission state locally.

## 3. Persistent Visual Design Rules (Tactical Operations-Console Aesthetic)
Operation Shadow uses a dark, high-density **tactical operations-console aesthetic** designed for robotics mission control — never a generic consumer AI/SaaS dashboard or chatbot template. All frontend components and layouts must adhere to `docs/ui-design-spec.md`, `docs/PRODUCT_LANGUAGE.md`, and the following rules:
1. **Surface Palette**: Use dark obsidian and charcoal (`#08090A` to `#1A1D20`) backgrounds with high-contrast, low-glare matte surfaces suitable for low-light operational environments.
2. **Operational Accents**: Use muted olive-drab (`#4A5D3F`), operational olive (`#5C744D` / `#5A8A6A`, retained in CSS variable `--accent-military` for compatibility), and desaturated sage (`#7AAA8A` / `#8AA37B`) accents for active states, spatial overlays, and operational indicators.
3. **Typography & Data Readouts**: Use warm off-white primary text (`#E2E4DE` / `#E6E8E1`) paired with strict monospaced typography (`tabular-nums`) for all telemetry values, `x/y/alt` coordinates, battery levels, timestamps, mission IDs, and vehicle identifiers (`SCOUT01`).
4. **Command-Console Layout & Framing**: Build compact operational panels with sharp or minimally radiused corners (`2px–3px`), thin hair-line borders (`1px solid`), subtle coordinate grid lines, and a dense, high-signal desktop command-console layout that avoids unnecessary page scrolling. Preserve existing `.c2-*` CSS classes and test selectors for backward compatibility.
5. **Alert Hierarchy & Emergency Stop**: Use high-visibility amber (`#C8882A` / `#D98A2B`) for warnings, degraded links, stale streams, unconfirmed coordinate frames, and `UNKNOWN` heartbeat states, and reserve a clearly distinguishable, unmistakable crimson/red control (`#C43030` / `#E04040`) exclusively for the Emergency Stop (`POST /emergency-stop`) control and critical faults.
6. **Prohibited Visual Patterns**: Strictly avoid purple/indigo gradients, cyberpunk neon effects, excessive glassmorphism/blur, oversized rounded SaaS cards, decorative AI sparkles/avatars/imagery, and generic consumer dashboard templates.

## 4. Tech Stack & Conventions
- **Backend**: Python 3.13+, FastAPI, SQLAlchemy 2.0 (async) with `aiosqlite`, Pydantic v2 / `pydantic-settings`.
- **Frontend**: Vite, React, TypeScript, React Context + `useReducer` for state management (no Redux, no Leaflet/Mapbox).
- **Important Python Import Note**: The backend application lives in `backend/fastapi/` per §3 of the architecture specification. Never place an `__init__.py` at `backend/fastapi/__init__.py` so that `backend/fastapi` does not shadow the installed `fastapi` PyPI library. Always run backend commands with `PYTHONPATH=fastapi:.` from the `backend/` directory (configured automatically for `pytest` and `mypy` in `backend/pyproject.toml`).

## 5. Verification Commands
Before completing any milestone, run and verify all checks:

### Root Makefile Suites
```bash
make test-architecture
make test-ultron
make test-backend
make test-frontend
```

### Direct Backend & Frontend Checks
```bash
cd backend
PYTHONPATH=fastapi:.:.. .venv/bin/pytest
PYTHONPATH=fastapi:.:.. .venv/bin/mypy fastapi ultron_stub tests

cd ../frontend
npm run typecheck
npm test -- --run
npm run build
```
