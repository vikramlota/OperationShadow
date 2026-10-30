# OPERATION SHADOW

## Product Requirements Document — v2.0

**Version:** 2.0 **Date:** September 2026 **Product Type:** Autonomous multi-vehicle AI mission system (ground + aerial) **Target:** Hackathon MVP — Demo Date **October 31, 2026** **Build Window:** October 1 – October 31, 2026

> **v2.0 changelog:** Adds **HAWK**, an aerial vehicle operating alongside **SCOUT**, both dispatched by **ULTRON**. Revises P0/P1 scope based on a build-risk review against the four-week timeline. Local on-robot SLM is demoted from P0 to a stretch goal. Adds dashboard and reliability features (reasoning trace, mission replay, simulated fallback mode) designed specifically to protect the demo against live-hardware failure.

---

## 1. Product Vision

OPERATION SHADOW is a physical AI system in which a human gives a **high-level mission**, and a mission commander coordinates **two heterogeneous embodied agents** to carry it out — rather than a human manually operating either vehicle.

The system consists of:

- **ULTRON** — the AI mission commander, running on a laptop.
- **SCOUT** — a ground robot capable of sensing, navigating, and interacting with its environment.
- **HAWK** — an aerial vehicle providing an elevated vantage point: patrol, hover-and-scan, and visual reconnaissance.

The core idea:

> **Human gives the objective → ULTRON plans across both vehicles → SCOUT and HAWK perceive and act → observations return to ULTRON → ULTRON adapts the mission, potentially redirecting either vehicle based on what the other discovers.**

The central demonstration is no longer just "a smart robot." It is **one commander reasoning over two different bodies with different capabilities, and re-planning across them when reality changes.**

---

## 2. Problem

Most robotics systems require a human to give detailed, vehicle-specific commands, and most demos that use more than one robot simply run them independently with no shared intelligence. OPERATION SHADOW instead accepts a single objective such as:

> "Inspect this unknown area, find the target, identify hazards, and return with a report."

ULTRON determines which subtasks go to SCOUT, which go to HAWK, and adapts the plan for **either** vehicle when new information arrives from **either** vehicle.

---

## 3. Target Users

### Primary (real-world framing)

- Disaster-response teams
- Industrial and infrastructure inspection teams
- Search-and-rescue operators
- Hazardous-environment workers

### Hackathon Demonstration User

A human operator/judge who gives ULTRON a mission through a dashboard or natural-language interface, and watches SCOUT and HAWK carry it out.

---

## 4. Core User Story

### User

> "ULTRON, inspect the building. Find the missing object, identify any hazards, and return."

### System

1. ULTRON understands the mission and breaks it into subtasks.
2. ULTRON assigns an aerial scan task to HAWK and a ground exploration task to SCOUT.
3. HAWK flies to a vantage point and streams video.
4. HAWK's feed is analyzed (on the laptop) and detects a hazard near SCOUT's planned route.
5. HAWK reports the observation to ULTRON.
6. **ULTRON replans SCOUT's route** to avoid the hazard zone, without any human intervention.
7. SCOUT continues, locates the target, and returns.
8. ULTRON generates a mission report covering both vehicles.

This cross-vehicle replanning step (Hawk's discovery changing Scout's plan) is the single most important moment in the demo — it is the clearest possible evidence of coordinated reasoning rather than two robots running independent scripts.

---

## 5. Product Objectives

### 5.1 P0 — Must Have

**Natural-language mission input.** A user can give a mission in plain language (e.g., "Explore the area, locate the red package, check for hazards, and return").

**Autonomous mission planning across two vehicles.** ULTRON converts the objective into structured tasks, each tagged with the vehicle type it applies to.

```text
MISSION
│
├── HAWK: Aerial scan of building exterior/interior sightlines
├── SCOUT: Explore ground-level sectors
├── SCOUT: Search for target
├── (either): Investigate detected hazards
├── Record observations
└── SCOUT: Return to starting point
```

**SCOUT autonomous ground navigation.** Move, avoid obstacles, reach assigned waypoints, return home. (See TRD for the specific algorithm choice — kept deliberately simple for reliability.)

**HAWK basic aerial dispatch.** Fly to a pre-defined waypoint or hold a fixed hover position, stream live video, and return/land on command or on low battery. HAWK's flight behavior is **pre-programmed and deterministic** — it does not do its own free-form autonomous navigation or obstacle avoidance beyond basic safety limits.

**Environmental perception.** From SCOUT and/or HAWK's camera and sensors: objects, humans, obstacles, temperature anomalies, gas/smoke or other environmental hazards, distance, and vehicle position.

**Mission replanning — including cross-vehicle.** ULTRON must react to unexpected events reported by *either* vehicle and adjust the plan for *either* vehicle. Example: HAWK spots a hazard from above → ULTRON reroutes SCOUT on the ground.

**Live dashboard.** Displays, at minimum:

- Mission objective and current task, per vehicle
- SCOUT position on a map; HAWK position/altitude
- Live camera feed (SCOUT and/or HAWK — whichever is actively relevant)
- Sensor readings, battery for both vehicles
- Connection status for both vehicles
- Detected objects/hazards
- Event log

```text
┌────────────────────────────────────────────────────┐
│                  OPERATION SHADOW                   │
├───────────────────────┬──────────────────────────────┤
│                       │ MISSION                       │
│        MAP            │ Inspect Building              │
│   🤖 SCOUT  🚁 HAWK   │                                │
│                       │ SCOUT: Search Sector B        │
│                       │ HAWK: Hover-scan Zone 2       │
├───────────────────────┼──────────────────────────────┤
│ CAMERA (active feed)  │ STATUS                        │
│                       │ SCOUT batt: 78%  Temp: 31°C   │
│       LIVE            │ HAWK  batt: 61%  Alt: 4.2m    │
├───────────────────────┴──────────────────────────────┤
│ EVENT LOG                                            │
│ 16:32 HAWK: hazard detected, Zone 2                  │
│ 16:33 ULTRON: replanning SCOUT route                 │
│ 16:33 SCOUT: rerouted, resuming search               │
└────────────────────────────────────────────────────┘
```

**Reasoning-trace panel.** Each planning/replanning event in the log includes a short human-readable rationale from ULTRON (e.g., "Route to Sector B blocked by hazard reported by HAWK → rerouting via corridor C"). This is what makes the agentic reasoning legible to judges in real time rather than something they have to infer.

**Simulated fallback mode.** ULTRON's world model supports a toggle per vehicle: real hardware, or a simulated vehicle feeding synthetic telemetry/observations. If either physical vehicle misbehaves on demo day, the operator can switch it to simulation and still demonstrate the full planning/replanning loop live.

---

### 5.2 P1 — Should Have

- **Mission memory** — ULTRON retains completed/pending tasks, both vehicles' positions, known objects/hazards, and explored areas across the mission.
- **Vehicle failure handling** — for both SCOUT and HAWK: communication loss, low battery, blocked route (SCOUT) or geofence/wind event (HAWK). ULTRON detects and adapts the mission (e.g., auto-return-home).
- **Mission replay/scrubber** — after a run, the operator can scrub back through the event log and see the plan-before / plan-after state at the replanning moment. Costs little beyond what's already logged, and turns the best moment of the demo into something judges can review rather than catch once, live.
- **Cached mission plan fallback** — for the specific rehearsed demo mission, a cached ULTRON plan is available if the live LLM call is slow or unavailable (venue Wi-Fi, rate limits). Live replanning still uses a live call; only the *initial* plan has a fallback.

---

### 5.3 P2 — Stretch Goals

- **Local robot intelligence (on-device SLM)** — a lightweight local model on SCOUT's compute for local semantic reasoning over its own sensor readings. *(Demoted from v1.0's P0 — see Section 12, Build Timeline, for rationale. Attempt only after the P0 cross-vehicle demo loop is reliable.)*
- **Physical interaction** — SCOUT gripper/arm to retrieve a found object.
- **HAWK payload or drop capability.**
- **Thermal camera, gas sensor, LiDAR** — additional sensing modalities beyond the MVP set.

---

## 6. Non-Goals

The MVP will **not** attempt to build:

- A humanoid robot or general-purpose AGI
- Fully unrestricted autonomous physical interaction
- Weapon systems, covert surveillance, autonomous hacking, or real-world espionage
- Perfect SLAM in arbitrary environments
- LLM-controlled motor or flight-control signals (see TRD, Critical Rule)
- **Beyond-visual-line-of-sight (BVLOS) flight**
- **Free-roaming autonomous flight navigation or aerial obstacle avoidance** — HAWK's flight is deliberately constrained to pre-programmed waypoints/hover points and a geofence, not general autonomous flight
- Flight over judges, spectators, or outside a rehearsed, geofenced area

The "Secret Agent" naming (ULTRON, SCOUT, HAWK) is a cinematic identity for a civilian autonomous inspection/rescue system, not actual covert operations.

---

## 7. Primary Demonstration Scenario

### Mission

> "Inspect this unknown building. Find the target, identify hazards, and return."

```text
Stage 1 — Mission given by human operator
Stage 2 — ULTRON plans: HAWK aerial scan + SCOUT ground exploration
Stage 3 — HAWK flies to vantage point, streams video; SCOUT begins exploring
Stage 4 — HAWK detects a hazard in SCOUT's path (from above)
Stage 5 — HAWK reports structured observation to ULTRON
Stage 6 — ULTRON replans SCOUT's route around the hazard zone
           (reasoning trace shown live on dashboard)
Stage 7 — SCOUT finds the target, returns
Stage 8 — ULTRON generates a mission report covering both vehicles
```

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
│         🚁 HAWK (hover)      │
│              🔥              │
│          HAZARD              │
└───────────────────────────────┘
```

This scenario is deliberately fixed and rehearsed rather than general-purpose — see Section 12 for why.

---

## 8. Success Metrics

| Metric | MVP Target |
| --- | --- |
| Mission understanding & decomposition | One reliable end-to-end run at demo time |
| SCOUT navigation | Reliable in the rehearsed demo environment |
| HAWK flight (waypoint/hover) | Reliable within geofence, no crash |
| Obstacle avoidance (SCOUT) | No collisions during rehearsed run |
| Hazard/target detection | Reliable for the specific rehearsed target/hazard |
| Cross-vehicle replanning | Demonstrable at least once, live |
| Dashboard latency | Near real-time |
| Cloud dependency | None beyond the LLM planning call |
| Human control | Mission-level only |

These are engineering targets for a **single rehearsed demo run**, not statistical claims about general performance — see Section 12 on why the plan optimizes for one excellent run over broad robustness.

---

## 9. Product Differentiator

The product is not:

> "A robot with an AI chatbot," or "two robots running separately."

It is:

> **A mission-driven system in which one AI commander reasons over two different physical bodies — ground and aerial — and adapts either vehicle's plan when the other vehicle discovers something new.**

The central demonstration is:

**Mission → Planning (across two vehicles) → Perception → Action → Cross-vehicle Observation → Replanning → Completion**

---

## 10. MVP Definition

The hackathon MVP is successful if the system can reliably demonstrate, in one rehearsed run:

1. Natural-language mission input.
2. ULTRON task planning across SCOUT and HAWK.
3. SCOUT autonomous ground navigation with obstacle avoidance.
4. HAWK waypoint/hover dispatch with live video.
5. Object/hazard detection from either vehicle's feed.
6. Vehicle → ULTRON structured reporting.
7. ULTRON → vehicle structured commands.
8. **Cross-vehicle dynamic replanning** (this is the demo's centerpiece).
9. Live dashboard with reasoning trace.
10. Final mission report.

**One excellent, rehearsed, cross-vehicle mission is worth more than either vehicle individually doing more things unreliably.**

---

## 11. Build Timeline (Oct 1 – Oct 31, 2026)

| Week | Dates | Focus | Exit criteria |
| --- | --- | --- | --- |
| 1 | Oct 1–7 | Skeleton, not polish. SCOUT drives (manual, over the same comms channel ULTRON will use). ULTRON stubbed as a FastAPI service returning a hardcoded plan. | SCOUT can be driven end-to-end through the real comms path. |
| 2 | Oct 8–14 | Autonomy skeleton. SCOUT reactive obstacle avoidance. Camera → laptop → object detection (not on-device). Real LLM call for mission decomposition. HAWK: basic waypoint/hover dispatch and video stream, integrated into the same schema. | SCOUT explores and avoids obstacles autonomously; HAWK holds a hover point and streams video; both report to ULTRON. |
| 3 | Oct 15–21 | The actual demo loop. One concrete cross-vehicle replanning trigger working end-to-end. Bare-bones dashboard (map, per-vehicle task/status, event log, reasoning trace). Mission report generation. | Full demo scenario (Section 7) runs at least once, end to end, even if rough. |
| 4 | Oct 22–31 | Harden, don't add. Feature-freeze by \~Oct 25. Rehearse the fixed scenario repeatedly. Build and test the simulated-fallback mode. Add mission replay/scrubber and cached-plan fallback. Only attempt P2 stretch goals (local SLM, gripper) if the core loop is rock-solid with days to spare. | The rehearsed demo run is reliable across repeated attempts; fallback mode tested and works. |

---

## 12. Design Rationale — Why Scope Is Constrained This Way

- **Local on-robot SLM demoted to P2.** Running detection *and* a local semantic model on constrained on-robot compute risks latency/thermal problems that eat build time without materially changing what judges see, since ULTRON's own reasoning is already visible via the trace panel. Vision and semantic interpretation run on the laptop for the MVP.
- **HAWK's autonomy is deliberately minimal.** Free autonomous flight navigation is a much harder and more failure-prone problem than ground navigation, with higher-cost failures (a crash). HAWK is treated as a smart camera on a stick that ULTRON dispatches to fixed points — the intelligence lives in ULTRON's planning, not in HAWK's flight control.
- **One rehearsed scenario, not general capability.** Judging rewards a convincing, reliable run over broad but fragile generality. Sensor thresholds, hazard placement, and target placement are tuned specifically for the one environment that will be demoed.
- **Simulated fallback exists because physical demos fail for reasons unrelated to the software.** The architecture's real selling point — ULTRON's planning and replanning — should be demonstrable even if a motor, prop, or radio link has a bad moment on stage.