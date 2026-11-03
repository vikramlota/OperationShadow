# Operation Shadow — Canonical Product Language & Terminology Guide (`docs/PRODUCT_LANGUAGE.md`)

**Status**: Canonical Reference (Phase 4 — Workstream B)  
**Applies To**: All documentation, UI copy, API descriptions, code comments, and future human/AI contributions.

---

## 1. Canonical Product Identity

**Operation Shadow** is a local robotics mission-control and environmental intelligence platform powered by **ULTRON**, coordinating the **SCOUT01** ground rover through a FastAPI backend, read-only ROS 2 perception integration, and a real-time React operations dashboard.

### Current Implementation Scope
- **Active Integrated Vehicle**: `SCOUT01` (ground rover).
- **In-Process Intelligence Agent**: `ULTRON` (LangGraph workflow, Event Router, Standing Orders, bounded Gemini LLM Triage & Planning with deterministic fallback, immutable Mission Memory snapshots, and deterministic After-Action Reports).
- **Default Operating Posture**: Simulated execution with hardware lockout engaged (`SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_DEFAULT_VEHICLE_MODE=simulated`, `SHADOW_SCOUT_SERIAL_ENABLED=false`, `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=false`).
- **Perception Integration**: Read-only ROS 2 Humble `rosbridge_suite` subscriber (`/detections_json` in camera-relative frame `oak_rgb_camera_optical_frame` / `oak_parent_frame`) plus direct browser-to-rover MJPEG / ROS camera and 2D LiDAR scan visualization.
- **Simulated / Roadmap Vehicle**: `HAWK01` (indoor aerial drone) remains a simulated / roadmap concept from the original v2.0 specification and is **not** an integrated operational vehicle.

---

## 2. Canonical Vocabulary Guidelines

Use the following preferred terminology when writing or updating user-facing copy, documentation, comments, and descriptions:

| Old / Potentially Misleading Wording | Preferred Canonical Wording |
|---|---|
| Military C2 System | Robotics Mission-Control System |
| Military Command Center | Robotics Operations Console |
| Field-grade military workstation | Professional operational command interface |
| Military tactical dashboard | Tactical-style robotics operations dashboard |
| Tactical C2 Dashboard | Mission-Control Dashboard / Operations Dashboard |
| Tactical C2 Backend | Mission-Control Backend |
| Military Green (`#5C744D`) | Operational Olive (`#5C744D`) |
| Multi-vehicle operational fleet | `SCOUT01` active vehicle integration (`HAWK01` simulated / roadmap) |
| HAWK integrated vehicle | `HAWK01` simulated / roadmap (not integrated) |
| Autonomous physical mission execution | Mission planning and simulated execution (or supervised serial command dispatch when explicitly enabled) |
| Live hardware integration | Identify the exact verified integration (e.g., read-only ROSBridge `/detections_json` perception or one-byte ESP32 USB serial transmitter contract) |
| Real-time autonomous intelligence | Evidence-grounded mission intelligence |

---

## 3. Core Domain Definitions & Usage Rules

1. **ULTRON**: The in-process mission intelligence agent running inside the FastAPI backend (`ultron/` + `backend/fastapi/services/ultron_agent_adapter.py`). ULTRON decomposes mission objectives into validated tasks, routes environmental/system events, applies deterministic Standing Orders, performs bounded LLM event triage, maintains the World Model and Mission Memory snapshots, and generates deterministic After-Action Reports.
2. **SCOUT01**: The sole active ground rover in the ULTRON runtime, FastAPI vehicle service, and React operations dashboard.
3. **HAWK01**: The aerial drone from the historical PRD/TRD v2.0 design. Must **always** be qualified as **simulated / roadmap, not integrated** whenever mentioned in current documentation or configuration comments.
4. **Robotics Mission Control / Operations Console**: The primary product category for Operation Shadow and its React operator interface.
5. **Mission**: A complete operational objective submitted by an operator (`POST /mission`), tracked through lifecycle states (`PENDING`, `PLANNING`, `ACTIVE`, `IN_PROGRESS`, `DEGRADED`, `COMPLETED`, `ABORTED`, `FAILED`).
6. **Task**: An individual unit of work assigned to a vehicle (`SCOUT01`) within a mission (`NAVIGATE`, `EXPLORE`, `SCAN`, `INSPECT`, `AVOID`, `STOP`, `RETURN`, `REPORT`).
7. **Observation**: Validated sensor-derived information ingested into the World Model and SQLite `observations` table, carrying explicit coordinate-frame, confidence, freshness (`is_stale`), and source-mode (`simulated` vs `physical`) provenance.
8. **Event**: A meaningful system, safety, telemetry, or environmental occurrence recorded in the event log (`events` table) or routed through ULTRON's `EventRouter`.
9. **Standing Order**: A deterministic operational policy (`configs/standing_orders.json`, e.g., `SO-01-CRITICAL-BATTERY`, `SO-02-SENSOR-CONFLICT`, `SO-03-BLOCKED-ROUTE`, `SO-04-HAZARD-HOLD`) executed immediately without calling an LLM when preconditions and World Model evidence are satisfied.
10. **Triage**: Bounded LLM-assisted (or deterministic fallback) interpretation of ambiguous events (`EventTriageResult`) that do not match a Standing Order.
11. **Simulation**: Any mission execution, task completion, telemetry update, or emergency-stop acknowledgement produced by `MockFleetSimulator`, `MockVehicleAdapter`, `StubUltronAgent`, or `POST /mission/{id}/complete`. Simulated outcomes must always be labeled `SIMULATED` and never described as physical rover execution.
12. **Physical Verification**: Reserved strictly for hardware behaviors that a human engineer has empirically verified on the physical rover (such as an independent onboard USB-loss motor-stop failsafe or calibrated TF extrinsics).

---

## 4. Visual Identity Preservation Rules

Phase 4 refines **product language**, not visual design. The dark tactical operations-console visual identity remains a core strength of Operation Shadow:

### Keep
- Dark obsidian, charcoal, and matte graphite operational surfaces (`#08090A` to `#1A1D20`).
- Operational olive (`#4A5D3F`, `#5C744D`) and desaturated sage (`#7AAA8A`, `#8AA37B`) accents.
- Compact, high-density telemetry panels with sharp corners (`2px–3px`) and `1px solid` hairline borders.
- Strict monospaced typography (`tabular-nums`) for coordinates, battery percentages, timestamps, sequence numbers, and IDs.
- High-visibility amber (`#C8882A` / `#D98A2B`) for warnings, stale streams, unconfirmed coordinate frames, and `UNKNOWN` heartbeat states.
- Unmistakable crimson/red (`#C43030` / `#E04040`) reserved for the Fast-Path Emergency Stop control and critical faults.
- Spatial SVG floor-plan and read-only 2D LiDAR scan overlays.

### Never Do
- Do **not** redesign the interface into a generic consumer SaaS dashboard, light-mode admin template, or chat-bubble AI assistant.
- Do **not** rename technical identifiers (`.c2-*` CSS classes, `--accent-military`, `hawk_geofence`, REST routes, Pydantic fields, or test IDs) merely to purge legacy terminology.

---

## 5. Prohibited Misleading Claims

Contributors and AI agents must **never** write documentation, UI copy, or comments that claim:
1. Operation Shadow is a military-certified or field-deployed military command-and-control system.
2. `HAWK01` is an active, integrated operational vehicle in the current runtime.
3. Simulated task completion (`SIMULATED_COMPLETED`) or open-loop serial command dispatch (`COMMAND_DISPATCHED`) is verified closed-loop physical rover execution (`PHYSICAL_EXECUTION_CONFIRMED`).
4. Camera-relative detections (`oak_rgb_camera_optical_frame`, `oak_parent_frame`) are global floor-map coordinates unless `SHADOW_ROS_MAP_FRAME_CONFIRMED=true` is calibrated and verified.
5. LiDAR integration into ULTRON's World Model (`SHADOW_LIDAR_ENABLED`) or the ESP32 USB-loss motor failsafe (`SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED`) is verified while those flags remain `false`.
