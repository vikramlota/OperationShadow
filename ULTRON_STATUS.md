# ULTRON Agent Status Report

## ✅ ULTRON IS CONFIGURED AND READY

ULTRON is **NOT a separate process**. It runs **inside the FastAPI backend** as an imported Python module.

---

## Architecture

```
FastAPI Backend (port 8000)
  │
  ├─ MissionService
  │   └─ UltronLangGraphAgent (imported from ultron/)
  │       ├─ Planning Engine (Gemini API)
  │       ├─ Safety Validator
  │       ├─ Task Assignment
  │       └─ Replanning Evaluator
  │
  └─ VehicleService
      └─ Sends commands to SCOUT01
```

---

## Configuration Status

✅ **Module Import**: ULTRON successfully imports from `ultron/`
✅ **LangGraph State Machine**: Built successfully
✅ **Hardware Lockout**: Enabled by default (`SHADOW_HARDWARE_LOCKOUT=true` in `core/config.py` and `.env.example`)
✅ **Vehicle Mode**: Simulated by default (`SHADOW_DEFAULT_VEHICLE_MODE=simulated` in `core/config.py` and `.env.example`)
✅ **LLM Model**: `gemini-2.5-flash`
✅ **Planning Timeout**: 10 seconds (`SHADOW_LLM_PLANNING_TIMEOUT_SEC=10`)
✅ **Replanning Timeout**: 8 seconds (`SHADOW_LLM_REPLANNING_TIMEOUT_SEC=8`)

> **Note:** Repository code (`core/config.py`) and `.env.example` default to `SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_DEFAULT_VEHICLE_MODE=simulated`, `SHADOW_SCOUT_SERIAL_ENABLED=false`, and `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=false`. A developer's local `.env` may differ from `.env.example` and must be checked before starting the backend.

---

## How ULTRON Starts

ULTRON initializes automatically when the FastAPI backend starts:

1. **Backend starts** → `uvicorn fastapi.main:app`
2. **MissionService initializes** → Imports `UltronLangGraphAgent`
3. **ULTRON builds LangGraph** → State machine ready
4. **ULTRON waits for missions** → Ready to plan

---

## ULTRON Workflow

### Mission Creation (POST /mission)
```
User → Dashboard → Backend API → MissionService
                                     ↓
                           UltronLangGraphAgent.plan_mission()
                                     ↓
                           ULTRON LangGraph executes:
                             1. Planner (Gemini API call)
                             2. Safety Validator
                             3. Task Assignment
                                     ↓
                           Returns: MissionPlan with tasks
                                     ↓
                           Backend → Stores in SQLite
                                   → Dispatches to SCOUT01
                                   → Broadcasts to Dashboard
```

### Replanning (Environmental Triggers)
```
SCOUT01 → ROS 2 Topic → ROSBridge → Backend → UltronLangGraphAgent.observe()
                                                       ↓
                                     ULTRON Evaluator checks triggers:
                                       - Hazards detected
                                       - Targets found
                                       - Battery low
                                                       ↓
                                     If trigger → Replan workflow
                                                       ↓
                                     New plan → Update tasks → SCOUT01
```

---

## Checking ULTRON Status

### When Backend is Running
```bash
# Check health endpoint
curl http://localhost:8000/health | jq '.ultron_is_stub'
# Should return: false (real ULTRON, not stub)
```

### Test ULTRON Planning
```bash
# Create a test mission
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{
    "objective": "Navigate to waypoint alpha and report status",
    "assigned_vehicles": ["SCOUT01"]
  }'

# ULTRON will:
# 1. Call Gemini API to generate plan
# 2. Validate plan against safety constraints
# 3. Assign tasks to SCOUT01
# 4. Return mission ID and task breakdown
```

---

## ULTRON Components

### 1. Planner (`ultron/planner/engine.py`)
- Calls Gemini API for plan generation
- Uses structured outputs (Pydantic schema)
- Falls back to cached plan if API fails
- **Current Issue**: SSL timeout (see bugfix we started earlier)

### 2. Safety Validator (`ultron/safety/validator.py`)
- Checks battery levels
- Validates waypoint reachability
- Ensures task feasibility
- Enforces physical constraints

### 3. Task Assigner (`ultron/graph/nodes.py`)
- Distributes tasks to vehicles
- Updates World Model
- Marks hazards as handled

### 4. Replanning Evaluator (`ultron/evaluator/triggers.py`)
- Monitors environmental changes
- Detects hazards, targets, battery events
- Triggers replanning when needed

---

## Known Issues

### ⚠️ Gemini API SSL Timeout
- **Status**: Active (see error output from `python ultron/main.py`)
- **Impact**: ULTRON falls back to cached plans
- **Workaround**: System operates in degraded mode using pre-recorded plans
- **Fix**: Bugfix workflow was started but paused per your request

### 🔒 Hardware Lockout & Simulated Mode Default
- Hardware lockout is enabled by default (`SHADOW_HARDWARE_LOCKOUT=true`)
- Default vehicle mode is `simulated` (`SHADOW_DEFAULT_VEHICLE_MODE=simulated`)
- Physical serial control (`SHADOW_SCOUT_SERIAL_ENABLED=false`, `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=false`) is disabled by default
- A developer's local `.env` may differ and must be checked before starting the backend (see `docs/SUPERVISED_REAL_MODE.md`)

---

## Verifying ULTRON When Backend Starts

Watch for these log messages:

```
INFO:     Operation Shadow backend initialized.
INFO:     UltronLangGraphAgent initialized
INFO:     Planning engine ready (Gemini: gemini-2.5-flash)
INFO:     Safety validator active
INFO:     World Model initialized (SCOUT01: ONLINE)
```

If you see these, ULTRON is running correctly.

---

## Testing ULTRON Standalone

You can test ULTRON independently (without the backend):

```bash
# Run ULTRON simulation test
python ultron/main.py

# This will:
# - Initialize World Model
# - Create a demo mission
# - Generate initial plan (or use cached)
# - Simulate hazard detection
# - Trigger replanning
# - Complete mission
```

**Note**: This currently has SSL timeouts, but the fallback mechanism works.

---

## Summary

**ULTRON Status**: ✅ Ready
**Integration**: ✅ Embedded in FastAPI backend
**Configuration**: ✅ Simulated mode default (`SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_DEFAULT_VEHICLE_MODE=simulated`; verify local `.env`)
**LangGraph**: ✅ Built and functional
**Planning Engine**: ⚠️ Using cached plans (API timeout issue)

**To Activate**: Start the FastAPI backend
**To Test**: Create a mission via POST /mission

---

Generated: $(date)
