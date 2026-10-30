# OPERATION SHADOW

## Technical Requirements Document — v2.0

**Version:** 2.0 **Architecture:** Laptop + Raspberry Pi (SCOUT) + ESP32/Arduino (SCOUT drivetrain) + Flight Controller (HAWK) **Deployment:** Local network / no cloud (except the LLM planning call)

> **v2.0 changelog:** Adds **HAWK** as a second, aerial vehicle under ULTRON's command. Simplifies the communication stack from MQTT+WebSocket+serial to a single WebSocket channel end-to-end, with serial retained only for the ESP32 hop. Defers occupancy-grid/A\* navigation and the on-robot local SLM to stretch goals. Adds resilience features (simulated vehicle mode, cached mission plan, reasoning-trace logging) as first-class requirements, not afterthoughts.

---

## 1. System Architecture

```text
                                   HUMAN
                                     │
                              Natural Language
                                     │
                                     ▼
                          ┌──────────────────┐
                          │      ULTRON       │
                          │      LAPTOP       │
                          │                   │
                          │ LLM (mission call) │
                          │ Mission Planner    │
                          │ World Model         │
                          │ Memory               │
                          │ Replanning Engine    │
                          │ Vision (both feeds)   │
                          │ FastAPI               │
                          │ Dashboard              │
                          └─────────┬─────────────┘
                                    │
                              WebSocket (Wi-Fi)
                          ┌─────────┴─────────┐
                          │                   │
                          ▼                   ▼
                ┌──────────────────┐ ┌──────────────────┐
                │      SCOUT        │ │       HAWK         │
                │   Raspberry Pi     │ │  Flight Controller  │
                │                    │ │                      │
                │ Sensor Fusion       │ │ Waypoint/Hover Logic  │
                │ Navigation           │ │ Altitude/Geofence      │
                │ Reactive Avoidance    │ │ Battery Monitor          │
                │ Camera Streaming       │ │ Camera Streaming           │
                │ Comms                  │ │ Comms                       │
                └────────┬───────────────┘ └────────┬────────────────────┘
                         │ Serial/GPIO                │ (onboard link,
                         ▼                             │  e.g. Wi-Fi/telemetry)
                ┌──────────────────┐                  ▼
                │   ESP32/Arduino   │           ┌──────────────┐
                │                   │           │ Motors/ESCs   │
                │ Motor Controller   │           │ IMU/Baro       │
                │ Sensors             │           │ GPS (if outdoor)│
                │ Actuators             │           │ Safety Layer     │
                │ Safety Layer            │           └──────────────┘
                └──────────────────┘
                         │
                         ▼
                 PHYSICAL GROUND ROBOT
```

---

## 2. Core Design Principle

The system uses a hierarchical control architecture, applied identically to both vehicles.

### Level 1 — Mission Intelligence (ULTRON)

Understanding mission, planning, task decomposition **across both vehicle types**, global state, replanning, mission memory, coordination.

### Level 2 — Local Intelligence

- **SCOUT:** Local sensor fusion and reactive decision-making on the Pi. A local SLM for semantic interpretation is a **P2 stretch goal** — for the MVP, structured sensor observations are sent to ULTRON directly for interpretation.
- **HAWK:** No local semantic intelligence. HAWK executes pre-programmed waypoint/hover behavior and reports raw telemetry/video; all reasoning about what it sees happens on the laptop.

### Level 3 — Perception

Vision and sensor interpretation. For the MVP, **both** SCOUT's and HAWK's camera feeds are processed on the laptop, not on-device, to avoid on-robot compute/thermal constraints during the build window.

### Level 4 — Navigation / Flight

- **SCOUT:** Deterministic ground navigation — waypoint following + reactive obstacle avoidance.
- **HAWK:** Deterministic flight control — waypoint/hover-point execution, altitude hold, geofence enforcement. **Not** general autonomous flight navigation or aerial obstacle avoidance.

### Level 5 — Safety

Deterministic, vehicle-specific safety controllers with final authority over motion for their vehicle: e-stop, velocity/altitude limits, collision/geofence prevention, battery-triggered auto-return, invalid-command rejection.

---

## 3. Critical Rule

## LLMs must NOT directly control motors or flight-control loops.

Bad architecture (either vehicle):

```text
LLM → "turn motor 37% for 2.4 seconds"
LLM → "pitch forward 12° for 800ms"
```

Correct architecture:

```text
ULTRON
   ↓
"Navigate to Sector B"              (SCOUT)
"Hold hover at Waypoint 2"          (HAWK)
   ↓
Navigation / Flight Controller
   ↓
"Move to (x,y)" / "Hold (x,y,alt)"
   ↓
Motor/ESC Controller
   ↓
Motor Commands
```

The LLM decides **what should happen**. The vehicle's deterministic controller determines **how to physically execute it**. This applies identically to SCOUT's motors and HAWK's flight surfaces/motors.

---

## 4. ULTRON Architecture

```text
ULTRON
│
├── Mission Interface
├── LLM (mission planning call)
├── Mission Planner (vehicle-aware task assignment)
├── Task Manager
├── World Model (per-vehicle state + shared hazard/target list)
├── Memory
├── Replanning Engine (cross-vehicle aware)
├── Vehicle Communication (SCOUT + HAWK)
├── Vision (both feeds)
├── Safety Validator
├── Mission Logger (includes reasoning-trace text)
├── Simulated Vehicle Mode (per-vehicle toggle)
└── FastAPI Server
```

---

## 5. ULTRON Mission Pipeline

```text
User Input
    ↓
Mission Parser
    ↓
Mission Representation
    ↓
Task Decomposition (tag each task: SCOUT / HAWK)
    ↓
Task Manager
    ↓
Vehicle Assignment (SCOUT and/or HAWK)
    ↓
Vehicle Observation (from either)
    ↓
World Model Update
    ↓
Evaluate Mission (cross-vehicle check)
    ↓
Continue / Replan (may affect either vehicle)
    ↓
Mission Completion
```

---

## 6. Structured Mission Representation

Do not allow the LLM to communicate with either vehicle using arbitrary text. Use structured commands, tagged by vehicle.

```json
{
  "mission_id": "M001",
  "objective": "inspect_building",
  "tasks": [
    {
      "id": "T001",
      "vehicle": "HAWK01",
      "type": "aerial_scan",
      "area": "zone_2",
      "priority": 1
    },
    {
      "id": "T002",
      "vehicle": "SCOUT01",
      "type": "explore",
      "area": "sector_A",
      "priority": 1
    },
    {
      "id": "T003",
      "vehicle": "SCOUT01",
      "type": "find_target",
      "target": "red_package",
      "priority": 2
    }
  ]
}
```

---

## 7. Vehicle Command Schema

### SCOUT example

```json
{
  "command_id": "CMD102",
  "robot_id": "SCOUT01",
  "vehicle_type": "ground",
  "action": "NAVIGATE",
  "target": { "x": 4.2, "y": 2.8 },
  "priority": "HIGH"
}
```

SCOUT actions: `EXPLORE, NAVIGATE, SCAN, INSPECT, FOLLOW, RETURN_HOME, PICK, DROP, STOP, REPORT`

### HAWK example

```json
{
  "command_id": "CMD201",
  "robot_id": "HAWK01",
  "vehicle_type": "aerial",
  "action": "HOVER_SCAN",
  "target": { "x": 4.2, "y": 2.8, "alt": 4.0 },
  "priority": "HIGH"
}
```

HAWK actions: `TAKEOFF, PATROL, HOVER_SCAN, RETURN_HOME, LAND, STOP, REPORT`

Notably absent from HAWK's action set: any free-navigation or path-planning action. HAWK only ever moves between pre-defined waypoints/hover points that were configured ahead of the demo, or returns/lands.

---

## 8. SCOUT Architecture

```text
SCOUT
│
├── Raspberry Pi
│   ├── Camera (streamed to laptop)
│   ├── Sensor Fusion
│   ├── Navigation (waypoint + reactive avoidance)
│   ├── Communication (WebSocket)
│   └── Robot State
│
└── ESP32/Arduino
    ├── Motors
    ├── Ultrasonic/ToF
    ├── IMU
    ├── Temperature
    ├── Gas/Smoke
    └── Safety Layer
```

*(Local SLM box from v1.0 removed from the core diagram — see Section 14, treated as a P2 addition only.)*

---

## 9. HAWK Architecture (new in v2.0)

```text
HAWK
│
├── Flight Controller (e.g., existing FC firmware — not built from scratch)
│   ├── IMU / Barometer
│   ├── GPS (if outdoor) or optical-flow/marker-based position hold (if indoor)
│   ├── Motors/ESCs
│   └── Safety Layer (geofence, min/max altitude, battery-triggered RTL/land)
│
├── Companion link (Wi-Fi telemetry to laptop)
│   ├── Camera streaming
│   ├── Telemetry (position, altitude, battery, status)
│   └── Command reception (waypoint/hover/land/RTL only)
```

**Assumption requiring validation before committing to this plan:** the team already has a flying-capable drone platform (frame + flight controller + motors tuned and flying). Building a flight-capable frame from scratch within this timeline is not advised — flight tuning (ESC calibration, prop balance, PID tuning) has historically consumed multiple weeks even for experienced teams, and crashes cost rebuild time that is not recoverable on a fixed hackathon deadline. If no such platform exists yet, HAWK should be treated as an optional stretch addition, not a Week 1 dependency.

---

## 10. Recommended Hardware

### Laptop

Runs ULTRON, LLM calls, FastAPI, dashboard backend, vision for both feeds, mission state/database.

### Raspberry Pi (SCOUT)

Camera streaming, sensor aggregation, navigation, communication.

### ESP32 (SCOUT drivetrain)

Motor control, sensor acquisition, low-level safety.

### HAWK flight platform

Existing flight controller (e.g., Betaflight/ArduPilot-class or a programmable ready-made platform) — **not built from scratch**. Wi-Fi or telemetry-radio link to the laptop for commands and video.

---

## 11. Sensor Layer

### SCOUT

- Navigation: ultrasonic, ToF, IMU, wheel encoders
- Environment: temperature, gas/smoke
- Vision: RGB camera

### HAWK

- Flight: IMU, barometer, GPS (outdoor) or optical-flow/marker position hold (indoor)
- Vision: RGB camera (primary sensing payload — HAWK's main job is being a vantage point)
- Battery voltage monitor (critical — feeds auto-RTL/land safety logic)

Do not add a sensor without a clear role in the rehearsed demo mission.

---

## 12. Sensor Fusion

Raw values are converted into structured observations before reasoning happens, regardless of which vehicle produced them:

```json
{
  "event": "possible_hazard",
  "source_vehicle": "HAWK01",
  "confidence": 0.87,
  "temperature": 43,
  "gas_level": "elevated",
  "visual_evidence": "smoke_like_pattern",
  "location": { "x": 5.1, "y": 3.0 }
}
```

Tagging `source_vehicle` is what makes cross-vehicle replanning traceable in the World Model and the reasoning-trace log.

---

## 13. Vision Pipeline

```text
Camera (SCOUT or HAWK)
   ↓
Frame Capture (streamed to laptop)
   ↓
Object Detection (laptop — YOLO-family lightweight detector / OpenCV)
   ↓
Confidence Filtering
   ↓
Structured Observation (tagged with source_vehicle)
   ↓
ULTRON World Model
```

Running detection on the laptop for both feeds avoids on-robot compute/thermal risk for both SCOUT and HAWK during the build window.

---

## 14. Local SLM (P2 — Stretch Goal Only)

If time remains after the P0 cross-vehicle demo loop is reliable (realistically, only in the final week), a lightweight local model may run on SCOUT's Pi for local semantic interpretation of its own sensor readings before reporting to ULTRON. **HAWK never runs local semantic reasoning** — its role is deliberately kept to flight execution and raw sensor/video reporting.

Potential models to benchmark if attempted: Qwen 0.5B/1.5B class, Gemma small models, other quantized edge-capable SLMs. Measure RAM usage, inference latency, structured-output reliability, and CPU temperature before committing — do not select by benchmark reputation alone.

---

## 15. Navigation System — SCOUT (Ground)

```text
Sensors → Reactive obstacle avoidance → Waypoint following → Motion Controller → Motors
```

For the MVP: **reactive avoidance + waypoint navigation**, not a full occupancy-grid/A\* planner. This is sufficient for a controlled, rehearsed demo environment and is significantly faster to get reliable within the build window. Occupancy-grid/A\* is a P2 addition only if Week 3's core loop is solid with time to spare.

---

## 16. Navigation System — HAWK (Aerial)

```text
Pre-configured waypoints/hover points
   ↓
Flight controller executes waypoint/hover/RTL/land
   ↓
Altitude hold + geofence enforcement (deterministic, onboard)
   ↓
Battery threshold → auto RTL/land (deterministic, onboard, cannot be overridden by ULTRON)
```

HAWK does **not** perform free autonomous navigation or its own obstacle avoidance for the MVP. Its flight envelope is a small, rehearsed set of waypoints/hover points within a geofence. This is the single most important scope constraint for HAWK — it is what makes the aerial vehicle buildable within the timeline instead of a second full robotics project.

---

## 17. Vehicle State

### SCOUT

```json
{
  "robot_id": "SCOUT01",
  "vehicle_type": "ground",
  "status": "EXPLORING",
  "battery": 78,
  "position": { "x": 3.4, "y": 2.1 },
  "heading": 90,
  "temperature": 31.4,
  "gas_level": "normal",
  "current_task": "T002",
  "connection": "ONLINE"
}
```

### HAWK

```json
{
  "robot_id": "HAWK01",
  "vehicle_type": "aerial",
  "status": "HOVER_SCAN",
  "battery": 61,
  "position": { "x": 4.2, "y": 2.8, "alt": 4.0 },
  "current_task": "T001",
  "connection": "ONLINE"
}
```

---

## 18. World Model

```text
WORLD MODEL
│
├── Vehicles
│   ├── SCOUT — position, battery, status
│   └── HAWK — position/altitude, battery, status
│
├── Environment
│   ├── Explored areas
│   ├── Obstacles
│   └── Map
│
├── Objects
│   ├── Target
│   └── Unknown objects (tagged with source_vehicle)
│
├── Hazards (tagged with source_vehicle)
│
└── Mission
    ├── Completed tasks
    ├── Active tasks (per vehicle)
    └── Pending tasks (per vehicle)
```

A structured state store (SQLite is sufficient) — no graph database needed for this scope.

---

## 19. Communication (simplified from v1.0)

**v1.0 recommended MQTT + WebSocket + serial. v2.0 simplifies this to a single WebSocket channel end-to-end** (laptop ↔ SCOUT's Pi, laptop ↔ HAWK's companion link), with serial retained only for the Pi→ESP32 hop. A single channel is materially faster to get reliable within a four-week build and is sufficient at this scale (two vehicles, one laptop). MQTT can be reintroduced later if pub/sub fan-out to more vehicles is ever needed — it is not required for this MVP.

```text
ULTRON ↔ WebSocket ↔ SCOUT (Pi) ↔ Serial ↔ ESP32
ULTRON ↔ WebSocket ↔ HAWK (companion link)
```

---

## 20. Communication Channels

```text
scout/01/command
scout/01/status
scout/01/observation
scout/01/sensor
hawk/01/command
hawk/01/status
hawk/01/observation
hawk/01/telemetry
ultron/mission
ultron/replan
```

---

## 21. Failure Handling

### SCOUT

```text
Heartbeat timeout → status = UNKNOWN → cancel active task → attempt reconnect
                                                             → if unavailable: mission degraded
Battery < 20% → finish safe subtask → RETURN_HOME
```

### HAWK

```text
Heartbeat/link timeout → status = UNKNOWN → trigger onboard failsafe (RTL or land — deterministic, not ULTRON-dependent)
Battery < threshold → auto RTL/land (onboard safety layer has final authority — cannot be delayed by ULTRON)
Geofence breach → auto RTL/land (onboard safety layer)
```

HAWK's safety failsafes are intentionally **onboard and deterministic**, not routed through ULTRON — a lost link must not require ULTRON to still be reachable in order for HAWK to land safely.

---

## 22. Safety Layer

Every movement/flight command passes through a vehicle-specific validator before reaching the motor/flight controller:

```text
ULTRON command → Command Validator → Safety constraints → Navigation/Flight Controller → Motors
```

### SCOUT safety constraints

Max speed, max acceleration, obstacle distance, e-stop, battery threshold, comms timeout, invalid-command rejection.

### HAWK safety constraints (additional)

Geofence boundary (hard limit, enforced onboard), min/max altitude, battery-triggered auto-RTL/land, no-fly zones (over judges/spectators — must be excluded from the geofence entirely, not just "avoided"), loss-of-link failsafe.

---

## 23. Dashboard Technology

```text
React → FastAPI → ULTRON
```

### Sections

- **Mission** — objective, current task per vehicle, progress, status
- **Vehicles** — position/altitude, battery, velocity, connection, current action (per vehicle)
- **Environment** — map, detected objects/hazards (tagged by source vehicle), sensor values
- **Vision** — live camera (switchable between SCOUT/HAWK feed)
- **Reasoning trace** — short natural-language rationale attached to each plan/replan event
- **Event log** — chronological, e.g. "SCOUT entered Sector A," "HAWK: hazard detected," "ULTRON: replanning SCOUT route"
- **Mission replay** — scrub back through the event log post-run, showing plan-before/plan-after at the replanning moment
- **Simulated mode toggle** — per-vehicle switch between real hardware and a simulated vehicle feeding synthetic telemetry/observations into the same World Model

---

## 24. Backend APIs

```text
POST /mission
GET  /mission/{id}
GET  /vehicle/{id}/status
GET  /vehicle/{id}/sensors
GET  /vehicle/{id}/observations
POST /vehicle/{id}/command
GET  /events
GET  /map
POST /vehicle/{id}/simulate   (toggle simulated mode for a vehicle)
POST /emergency-stop
```

---

## 25. Data Flow

```text
                HUMAN
                  │
                  ▼
             Mission Text
                  │
                  ▼
              ULTRON LLM
                  │
             Task Plan (per vehicle)
                  │
          ┌───────┴───────┐
          ▼               ▼
      SCOUT Task       HAWK Task
          │               │
   Camera/Sensors/Nav   Camera/Telemetry
          │               │
          └───────┬───────┘
                  ▼
           Observation (tagged source_vehicle)
                  │
                  ▼
           ULTRON Vision + World Model
                  │
             World Update
                  │
          ┌───────┴───────┐
          ▼               ▼
       Continue       Replan (either vehicle)
```

---

## 26. Repository Structure

```text
operation-shadow/
│
├── ultron/
│   ├── planner/
│   ├── memory/
│   ├── world_model/
│   ├── replanner/
│   ├── vision/
│   ├── communication/
│   ├── safety/
│   ├── simulation/          # simulated-vehicle fallback mode
│   └── main.py
│
├── scout/
│   ├── navigation/
│   ├── sensors/
│   ├── communication/
│   └── main.py
│
├── hawk/
│   ├── flight_interface/    # wraps existing FC firmware/telemetry
│   ├── sensors/
│   ├── communication/
│   └── main.py
│
├── firmware/
│   └── esp32/
│       ├── motors/
│       ├── sensors/
│       └── safety/
│
├── backend/
│   └── fastapi/
│
├── dashboard/
│   └── react/
│
├── configs/
│
├── tests/
│
└── README.md
```

---

## 27. Software Stack

| Layer | Technology |
| --- | --- |
| ULTRON LLM | LLM API call (with cached-plan fallback) |
| Agent orchestration | Custom state machine |
| Backend | FastAPI |
| Dashboard | React |
| Database | SQLite |
| Communication | WebSocket (+ serial for ESP32 hop) |
| Vision | YOLO/OpenCV (laptop, both feeds) |
| SCOUT robotics | Python + C++ where needed |
| SCOUT microcontroller | ESP32 |
| SCOUT compute | Raspberry Pi |
| HAWK flight control | Existing FC firmware (not built from scratch) |
| Mapping | Occupancy grid (P2 stretch — not MVP) |
| Path planning (SCOUT) | Reactive avoidance + waypoints (A\* deferred to P2) |
| Deployment | Local network |
| Cloud | None (except the LLM planning call) |

---

## 28. Agent State Machine

```text
IDLE
  ↓
UNDERSTAND
  ↓
PLAN (assign tasks across SCOUT/HAWK)
  ↓
ASSIGN
  ↓
EXECUTE
  ↓
OBSERVE (from either vehicle)
  ↓
EVALUATE
  │
  ├── SUCCESS → COMPLETE
  │
  ├── FAILURE → RECOVER
  │
  └── NEW INFORMATION → REPLAN
                         ↓
                       ASSIGN
```

One state machine, shared World Model, multiple vehicles — this is what makes the coordination legible and debuggable rather than "two independent robot loops."

---

## 29. Example Cross-Vehicle Replanning Logic

Initial plan:

```text
HAWK: hover-scan Zone 2   |   SCOUT: explore Sector A → find target → return
```

HAWK reports:

```text
Hazard detected in Zone 2, which overlaps SCOUT's planned route
```

ULTRON updates:

```text
SCOUT: explore Sector A → AVOID Zone 2 → find target via alternate route → return
```

Reasoning trace logged: *"HAWK detected a hazard (confidence 0.87) in Zone 2, which overlapped SCOUT's route to Sector B. Rerouting SCOUT via corridor C."*

This is the core agentic behavior the demo is built around.

---

## 30. Development Phases Mapped to Build Timeline

| Phase | Maps to | Deliverable |
| --- | --- | --- |
| 1 — Robot foundation | Week 1 | SCOUT drives manually over the real WebSocket path; ULTRON stub returns a hardcoded plan |
| 2 — Autonomy skeleton | Week 2 | SCOUT reactive avoidance; camera→laptop→detection; real LLM planning call; HAWK waypoint/hover dispatch + video stream |
| 3 — Demo loop | Week 3 | One cross-vehicle replanning trigger, end to end; bare-bones dashboard with reasoning trace; mission report |
| 4 — Harden | Week 4 (freeze \~Oct 25) | Repeated rehearsal of the fixed scenario; simulated-fallback mode; mission replay; cached-plan fallback; P2 stretch goals only with time to spare |

---

## 31. Hackathon Demo Architecture

```text
┌───────────────────────────────┐
│           BUILDING            │
│                               │
│  ┌──────┐       ┌────────┐   │
│  │ROOM A│       │ ROOM B │   │
│  │      │       │ TARGET │   │
│  └──┬───┘       └───┬────┘   │
│     │               │        │
│ ┌───┴───────────────┴─────┐  │
│ │       CORRIDOR          │  │
│ │          🤖             │  │
│ └─────────────────────────┘  │
│         🚁 (fixed hover pt)   │
│              🔥              │
│          HAZARD              │
└───────────────────────────────┘
```

Fixed, rehearsed environment: known route, known obstacle set, known target location, known hazard location, one pre-configured HAWK hover point with clear sightline to the hazard/route overlap.

---

## 32. Minimum Viable Hardware

```text
SCOUT:
  Raspberry Pi, ESP32, chassis, 2/4 DC motors, motor driver,
  battery, camera, ultrasonic/ToF, IMU, temperature sensor,
  one hazard sensor, wheel encoders

HAWK:
  Existing flying-capable platform (frame + flight controller + motors,
  already tuned — NOT built from scratch), camera, telemetry/Wi-Fi link,
  battery voltage monitor
```

**Critical dependency to validate immediately (Week 0, before Oct 1):** confirm a HAWK-capable drone platform already exists and flies. If not, HAWK should be descoped to a stretch goal rather than a P0 dependency — see PRD Section 12.

Optional (P2): thermal camera, gas sensor, servo arm/gripper, LiDAR.

---

## 33. Engineering Priorities

```text
1. Reliable SCOUT movement
2. Emergency stop (both vehicles)
3. SCOUT obstacle avoidance
4. Vehicle ↔ laptop communication (both)
5. Cameras (both)
6. Object/hazard detection (laptop-side, both feeds)
7. HAWK waypoint/hover dispatch (pre-programmed only)
8. Mission planning (ULTRON, vehicle-aware)
9. Cross-vehicle replanning
10. Dashboard (map, status, reasoning trace, event log)
11. Resilience: simulated-fallback mode, cached-plan fallback, mission replay
12. (P2 stretch) Local SLM on SCOUT
13. (P2 stretch) Physical manipulation / HAWK payload
```

A brilliant planner attached to vehicles that cannot move/fly reliably is still a failed demo — this order is intentional and unchanged in spirit from v1.0, extended to cover HAWK.

---

## 34. Final Technical Definition

OPERATION SHADOW v2.0 is a **hierarchical, multi-vehicle embodied-agent architecture**.

```text
                          ULTRON
                    Global Intelligence
                          │
             ┌────────────┼────────────┐
             │            │            │
          Planning      Memory      Replanning
             │            │            │
             └────────────┼────────────┘
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
          Mission Task             Mission Task
              │                       │
            SCOUT                   HAWK
       Local Intelligence      (Execution only —
              │                 no local reasoning)
    ┌─────────┼─────────┐               │
    ▼         ▼         ▼               ▼
 Vision   Sensors   (SLM: P2)      Camera / Telemetry
    │         │                          │
    └─────────┼──────────────────────────┘
              ▼
          Navigation / Flight
              ▼
         Safety Controller
              ▼
       Motors / Motors+ESCs
              ▼
         Physical World
              │
              └──────────────→ Observation (tagged by source vehicle)
                                          ↓
                                       ULTRON
```

### Core principle

**ULTRON thinks globally, across both vehicles.**

**SCOUT understands and moves locally on the ground.**

**HAWK executes pre-defined flight behavior and sees from above — it does not reason.**

**Each vehicle's safety layer has final, onboard authority over its own motion — independent of ULTRON's reachability.**

That separation is what makes the two-vehicle architecture both technically defensible and realistically buildable within a four-week hackathon window.