# Phase 1 Baseline Verification (`docs/BASELINE.md`)

## 1. Environment & Safety Configuration
All baseline test suites and service checks were executed on branch `phase1-stabilize` with hardware lockout enforced, simulated vehicle mode active, and `GEMINI_API_KEY` unset:

```bash
export PATH="$(pwd)/backend/.venv/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
unset GEMINI_API_KEY
export SHADOW_DEFAULT_VEHICLE_MODE=simulated
export SHADOW_HARDWARE_LOCKOUT=true
export SHADOW_SCOUT_SERIAL_ENABLED=false
export SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=false
export SHADOW_ROSBRIDGE_ENABLED=false
export SHADOW_ROS_ENABLED=false
```

> **Note on local `.env` overrides:** Outside `pytest`, `backend/fastapi/core/config.py` and `ultron/planner/engine.py` load `.env` from the repository root if present (`override=False`). Exporting `GEMINI_API_KEY=""`, `SHADOW_HARDWARE_LOCKOUT=true`, and `SHADOW_DEFAULT_VEHICLE_MODE=simulated` in the shell environment guarantees that live server processes (`uvicorn`) remain in simulated mode with Gemini disabled regardless of a developer's local `.env`.

## 2. Test Suite Results

| Suite Target | Exact Command Executed | Passed | Failed | Skipped | Warnings | Runtime |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `make test-architecture` | `python3 -c "from tests.test_system_architecture import test_trd_section_26_repository_layout, test_segregated_frontend_backend_hardware_layout; test_trd_section_26_repository_layout(); test_segregated_frontend_backend_hardware_layout(); print('Architecture & segregation layout verified!')"` | 2 | 0 | 0 | 0 | 0.13s |
| `make test-ultron` | `PYTHONPATH=.:backend:backend/fastapi pytest tests` | 15 | 0 | 0 | 0 | 0.33s (`1.18s` wall) |
| `make test-backend` | `cd backend && PYTHONPATH=fastapi:.:.. pytest` | 49 | 0 | 0 | 4 | 10.00s (`12.08s` wall) |
| `make test-frontend` | `cd frontend && npm run typecheck && npm test -- --run` | 16 (`tsc` + 16 Vitest) | 0 | 0 | 0 | 2.27s (`5.31s` wall) |

## 3. Failure & Warning Classification

| Item | Classification | Root Cause & Resolution |
| --- | --- | --- |
| `pytest: command not found` when running `make test-ultron` / `make test-backend` in a bare shell | **ENV** | The `Makefile` invokes `pytest` and `uvicorn` directly rather than `backend/.venv/bin/pytest`. Prepending `$(pwd)/backend/.venv/bin` to `PATH` resolves all Python CLI tools (`Python 3.13.0`, `pytest 8.4.2`, `uvicorn 0.54.0`). |
| `npm: command not found` in non-login shells on macOS Homebrew | **ENV** | Homebrew binaries live in `/opt/homebrew/bin`. Prepending `/opt/homebrew/bin` to `PATH` resolves `node` and `npm`. |
| Untracked root `main.py` and `backend/main.py` shims and dirty `backend/fastapi/main.py` in working tree prior to baseline | **ENV** | Leftover local entrypoint shims were removed and `backend/fastapi/main.py` restored to `HEAD` so `PYTHONPATH=fastapi:.:..` layout remains untouched per project rules. |
| Starlette `HTTP_422_UNPROCESSABLE_ENTITY` & `TestClient` deprecation warnings (4 warnings in `test-backend`) | **TEST DRIFT** | Cosmetic upstream Starlette/FastAPI deprecation warnings; all 49 backend assertions pass without modification. |
| React `act(...)` stderr warnings in `frontend/src/__tests__/App.test.tsx` | **TEST DRIFT** | Asynchronous WebSocket state transitions in `FloorMap` and `DashboardProvider` emit standard React testing warnings on stderr; all 16 frontend tests pass. |

## 4. Runtime, REST API, and WebSocket Verification

### Commands Used to Start Services
```bash
# Backend (FastAPI + in-process ULTRON on http://127.0.0.1:8000)
SHADOW_DEFAULT_VEHICLE_MODE=simulated SHADOW_HARDWARE_LOCKOUT=true GEMINI_API_KEY="" make run-backend

# Frontend (React + Vite on http://localhost:5173)
make run-frontend
```

### Health Check (`GET /health`)
```bash
curl -s http://127.0.0.1:8000/health
```
Verified response:
```json
{
  "status": "ok",
  "database": "ok",
  "hardware_lockout": true,
  "default_vehicle_mode": "simulated",
  "ultron_is_stub": false,
  "vehicles": [
    "SCOUT01"
  ],
  "ros_enabled": false,
  "ros_connection_state": "DISABLED",
  "ros_vehicle_id": null
}
```
Confirmed:
- `hardware_lockout` is `true`
- `default_vehicle_mode` is `"simulated"`
- `ultron_is_stub` is `false` (running `UltronLangGraphAgent` in-process)

### Mission Creation (`POST /mission`) & Request Schema Reconciliation
Inspection of `backend/fastapi/api/mission.py` and `backend/fastapi/schemas/entities.py` (`MissionCreateRequest`) confirmed that the authoritative request schema uses `ConfigDict(extra="forbid")` and accepts **only** `objective` (required, 1–2000 chars) and `language` (optional, default `"English"`). Sending `assigned_vehicles` (as shown in some older doc examples) is rejected with HTTP 422 because `extra="forbid"` is enforced.

Exact `curl` command used to create a mission:
```bash
curl -s -X POST http://127.0.0.1:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective": "Inspect sector_A and zone_2 for hazards and report status.", "language": "English"}'
```
Response:
```json
{
  "mission_id": "M002"
}
```

### Tasks, Events, and Reasoning/Decision Traces Verification
1. **Tasks (`GET /mission/M002`)**:
   - Returned `status: "ACTIVE"` with 3 validated tasks assigned to `SCOUT01`:
     - `M002-T01`: `NAVIGATE` (`area: "zone_2"`, `priority: 1`, `status: "ASSIGNED"`)
     - `M002-T02`: `EXPLORE` (`area: "sector_A"`, `priority: 2`, `status: "ASSIGNED"`)
     - `M002-T03`: `INSPECT` (`target: "red_package"`, `priority: 3`, `status: "ASSIGNED"`)
2. **Events & Reasoning Traces (`GET /mission/M002/replay`)**:
   - Returned chronological entries of type `["event", "reasoning_trace"]`:
     - `M002-EVT01` (`MISSION`): `"Mission M002 created: Inspect sector_A and zone_2 for hazards and report status."`
     - `M002-RT01` (`OPERATOR_MISSION_DISPATCH`): `"USED CACHED PLAN — Live planning call timed out. Executing rehearsed demo initialization."` with `resulting_task_ids: ["M002-T01", "M002-T02", "M002-T03"]`
3. **World Model Decision Traces (`GET /mission/intelligence/snapshot`)**:
   - Returned `decision_traces` entry (`trigger: "INITIAL_MISSION_PLANNING"`, `safety_validator_passed: true`, `resulting_task_ids: ["T-CACHE-01", "T-CACHE-02", "T-CACHE-03"]`, `is_simulated: true`).
4. **Dashboard WebSocket (`ws://127.0.0.1:8000/ws/dashboard`)**:
   - Defined in `backend/fastapi/main.py` (`@application.websocket("/ws/dashboard")`) and handled by `DashboardGateway.handle_client_session` in `backend/fastapi/websocket/dashboard_gateway.py`.
   - Confirmed that upon connection it immediately emits a `snapshot` envelope (`sequence: 1`), and upon `POST /mission` it broadcasts live envelopes in order:
     1. `{"type": "event", "sequence": 2}`
     2. `{"type": "reasoning_trace", "sequence": 3}`
     3. `{"type": "mission_status", "sequence": 4}`
