# Operation Shadow — UI & Operations Console Design Specification

**Scope:** Visual design system, color tokens, typography, component styling, layout architecture, and visual hierarchy for the Operation Shadow React robotics mission-control console (`frontend/src/`).  
**Canonical Terminology Reference:** [docs/PRODUCT_LANGUAGE.md](./PRODUCT_LANGUAGE.md)

## 1. Design Philosophy & Aesthetic Direction

Operation Shadow is a local robotics mission-control and environmental intelligence console coordinating the **SCOUT01** ground rover (with **HAWK01** retained as a simulated / roadmap aerial concept). Its interface is designed as a **professional operational command interface** and **tactical-style robotics operations console**:
- **High Information Density, Zero Visual Fluff**: Every pixel serves spatial situational awareness, sensor/telemetry inspection, evidence-grounded ULTRON reasoning transparency, or rapid operator intervention.
- **Low-Light Operational Ergonomics**: Matte near-black and dark charcoal surfaces prevent eye fatigue and make spatial vectors, amber caution states, and the crimson Emergency Stop control immediately legible.
- **Deterministic Readability**: Fixed-width tabular numerals ensure rapidly updating telemetry (`200–500ms` heartbeat cadence) and perception timestamps never cause layout jitter.
- **Provenance & Capability Honesty**: Simulated execution, hardware lockout posture, stale streams, camera-relative coordinate frames (`oak_rgb_camera_optical_frame`, `oak_parent_frame`), and unimplemented sensors are explicitly labeled at all times.

### Explicitly Prohibited Anti-Patterns
- **No AI/SaaS clichés**: No purple, violet, or cyan-to-magenta gradients; no "AI sparkle" icons, chat bubbles, or decorative robot illustrations.
- **No consumer card bloat**: No oversized border-radiuses (`> 4px`), heavy drop shadows, floating glassmorphic panels, or spacious marketing whitespace.
- **No neon cyberpunk tropes**: Avoid glowing text-shadows, saturated laser greens, or decorative HUD animations that do not represent real backend state.

## 2. Color Palette & CSS Custom Properties

All colors are defined as semantic CSS variables in `frontend/src/styles/tactical.css`. Legacy variable names (such as `--accent-military` and `--marker-hawk`) and `.c2-*` class aliases are intentionally preserved for backward compatibility:

| Token Category | CSS Variable | Hex Value | Operational Usage |
| --- | --- | --- | --- |
| **Canvas Background** | `--sf-void` / `--bg-canvas` | `#08090A` | Root viewport background, spatial map void |
| **Recessed / Input Surface** | `--sf-base` / `--bg-recessed` | `#0D0F10` | Command inputs, data cells, log viewports, camera letterbox |
| **Primary Panel Surface** | `--sf-raised` / `--bg-panel` | `#131618` | Standard operational module background (`MissionPanel`, `EventLog`, `ReasoningTracePanel`) |
| **Elevated / Header Surface** | `--sf-elevated` / `--bg-panel-header` | `#1A1D20` | Module header bars, global operations bar, scrub bar track |
| **Primary Border** | `--border-subtle` | `#1E2226` | `1px solid` panel framing and internal dividers |
| **Visible / Focus Border** | `--border-visible` / `--border-focus` | `#2A3038` / `#5A7A6A` | Interactive control borders, focused vehicle card border |
| **Primary Text** | `--text-primary` | `#E2E4DE` | Warm off-white primary labels, mission objectives, active readouts |
| **Secondary Text** | `--text-secondary` | `#8A8F8C` | Muted sage-gray field labels, secondary descriptions |
| **Muted / Disabled Text** | `--text-muted` | `#4E5553` | Inactive controls, historical timestamps, axis ticks |
| **Accent Primary (Olive Drab)** | `--accent-primary` / `--accent-olive` | `#5A8A6A` | Primary operational controls, active layer buttons, panel header accent bar |
| **Accent Secondary (Operational Olive)** | `--accent-active` / `--accent-military` | `#4A7A5A` | Nominal status badges (`ONLINE`, `ACTIVE`, `SIMULATED`), interactive borders *(retains `--accent-military` token name for compatibility)* |
| **Accent Highlight (Desaturated Sage)** | `--marker-scout` / `--accent-sage` | `#7AAA8A` | Active waypoint markers, `SCOUT01` vector path, selected timeline node |
| **Roadmap Aerial Marker** | `--marker-hawk` | `#7A9A95` | Desaturated slate-teal reserved for simulated / roadmap `HAWK01` styling |
| **Warning / Degraded (Amber)** | `--status-warning` | `#C8882A` | Low battery, missed heartbeat (`UNKNOWN` state), simulated-mode lockout banner, camera-relative coordinate warning |
| **Critical / Emergency Stop (Red)** | `--status-critical` | `#C43030` | Emergency Stop control fill, connection loss (`DISCONNECTED` fault), hazard markers |
| **Emergency Stop Active/Hover** | `--status-critical-bright` | `#E04040` | High-contrast border and active press state for `EmergencyStopButton` |

## 3. Typography & Telemetry Formatting

| Role | Font Stack | Size / Weight / Tracking | Application |
| --- | --- | --- | --- |
| **Brand & Display Headings** | `'Michroma', sans-serif` (`--font-display`) | `10px–12px`, uppercase, `letter-spacing: 0.12em` | Global header brand (`OPERATION SHADOW — MISSION CONTROL`) |
| **UI & Panel Headers** | `'Space Grotesk', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif` (`--font-ui`) | `11px–13px`, `600–700`, uppercase, `letter-spacing: 0.06em` | Panel titles (`MISSION COMMAND & TASKING`, `ROS PERCEPTION // SCOUT01`), button labels |
| **Telemetry & Operational Data (Monospace)** | `'IBM Plex Mono', 'SF Mono', 'Menlo', 'Consolas', monospace` (`--font-data`) | `10px–14px`, `500`, `font-variant-numeric: tabular-nums` | Vehicle IDs (`SCOUT01`), coordinates (`X: 2.40m Y: 5.10m`), battery `%`, timestamps (`23:55:18.240Z`), mission/task/snapshot IDs (`M001`, `M001-T01`, `SNAP-...`) |

### Formatting Rules
- All numeric telemetry (`battery`, `position_x`, `position_y`, `position_alt`, range, confidence, latency counters) must use `font-variant-numeric: tabular-nums` and fixed decimal precision so digits never shift horizontally during WebSocket pushes.
- Timestamps in `EventLog`, `ReasoningTracePanel`, and `MissionReplayScrubber` display in compact 24-hour UTC format (`HH:MM:SS.mmmZ`).
- Vehicle identifiers (`SCOUT01`) and status codes (`IDLE`, `ACTIVE`, `EXECUTING`, `UNKNOWN`, `EMERGENCY_STOPPED`, `FAULT`) are always rendered in uppercase monospace.

## 4. Layout Architecture & Visual Hierarchy (Active `SCOUT01` Console)

The console is designed for a `1440×900` to `1920×1080` desktop operator screen as a fixed-viewport 3-column grid with a top operations bar and a 3-panel bottom dock (`frontend/src/App.tsx`):

```text
+------------------------------------------------------------------------------------------------------+
| GLOBAL OPERATIONS BAR (42px height)                                                                  |
| [OPERATION SHADOW — MISSION CONTROL] [MISSION: M001] [API] [WS] [MODE: SIMULATED] [LOCKOUT] [E-STOP] |
+-------------------------------+----------------------------------------+-----------------------------+
| LEFT RAIL (320px)             | CENTER VIEWPORT (1fr)                  | RIGHT RAIL (380px)          |
|                               |                                        |                             |
| +---------------------------+ | +------------------------------------+ | +-------------------------+ |
| | MISSION COMMAND & TASKING | | | INDOOR FLOOR MAP / LIVE 2D LIDAR   | | | OPTICAL FEED // SCOUT01 | |
| | - Objective input & demo  | | | - FLOOR PLAN vs LIVE 2D MAP toggle | | | - Direct MJPEG or ROS   | |
| | - Assigned tasks list     | | | - Room walls & coordinate grid     | | |   camera stream         | |
| | - After-Action Report     | | | - Fixed waypoints & observations   | | +-------------------------+ |
| |   viewer & MD/JSON export | | | - Live SCOUT01 marker & trail      | | | SCOUT01 STATUS CARD     | |
| +---------------------------+ | | - Map file archival (.ply/.pcd/.db)| | | - Battery, X/Y/Alt, HB  | |
| | OPERATING MODE & LOCKOUT  | | +------------------------------------+ | | - Sensor snapshot sync  | |
| | - Hardware Lockout notice | |                                        | +-------------------------+ |
| | - ROS link status sync    | |                                        | | ROS PERCEPTION //SCOUT01| |
| | - SCOUT01 SIMULATED /     | |                                        | | - Camera/LiDAR status   | |
| |   REAL (LOCKED) buttons   | |                                        | | - Frame-separated objs  | |
| +---------------------------+ |                                        | | - Evidence Inspector &  | |
| | SCOUT01 MANUAL INPUT      | |                                        | |   ULTRON Query Box      | |
| | - W/A/S/D/X sim/serial pad| |                                        | +-------------------------+ |
+-------------------------------+----------------------------------------+-----------------------------+
| BOTTOM DOCK (180px–220px height, 3 equal columns)                                                    |
| +-------------------------------+ +----------------------------------+ +---------------------------+ |
| | MISSION REPLAY SCRUBBER       | | EVENT LOG                        | | REASONING TRACE           | |
| | Chronological timeline with   | | Category-filtered operational    | | Trigger, rationale,       | |
| | BEFORE / AFTER World Model    | | events & sync control            | | safety validator & free-  | |
| | snapshots & availability badge| |                                  | | space epistemic status    | |
| +-------------------------------+ +----------------------------------+ +---------------------------+ |
+------------------------------------------------------------------------------------------------------+
```

### Visual Hierarchy Priorities
1. **Priority 1 — Safety & Emergency Stop (`EmergencyStopButton.tsx`)**: Pinned to the top-right of the Global Operations Bar at all times. High-contrast crimson fill (`#C43030`), bold uppercase monospace label, two-step confirmation (`E-STOP ALL` / `E-STOP SCOUT01` -> `CONFIRM`), and explicit display of HTTP acceptance vs. dispatch attempt vs. vehicle acknowledgement vs. confirmed stopping.
2. **Priority 2 — Spatial Situational Awareness (`FloorMap.tsx`, `LidarScanView.tsx` & `VehicleStatusCard.tsx`)**: Center-stage SVG indoor floor plan (`GET /map`, explicitly badged `PROVISIONAL SIMULATION LAYOUT`) and switchable read-only 2D LiDAR scan view (`/cloud_registered`, `/path`), paired with the `SCOUT01` status card showing battery, connection health (`CONNECTED`, `UNKNOWN`, `DISCONNECTED`), mode (`SIMULATED` / `REAL`), and `x/y/alt` readouts.
3. **Priority 3 — Visual Confirmation & Evidence-Grounded Perception (`CameraFeed.tsx`, `PerceptionPanel.tsx`)**: Right-hand rail stacking the direct `SCOUT01` optical feed, multi-sensor status strip (Depth Camera + `UNAVAILABLE` LiDAR status), coordinate-frame-separated object detections (`oak_rgb_camera_optical_frame`, `oak_parent_frame`, `map`), interactive Evidence Inspector, and ULTRON environmental query box.
4. **Priority 4 — Post-Mission Replay & After-Action Intelligence (`MissionReplayScrubber.tsx`, `MissionPanel.tsx`, `EventLog.tsx`, `ReasoningTracePanel.tsx`)**: Bottom dock and mission report viewer allowing operators to scrub before/after World Model states across every decision and export deterministic After-Action Reports in Markdown or JSON.

### Note on Historical `HAWK01` UI Elements
- In the original v2.0 concept, the UI specification included a second vehicle card (`HAWK01`), a camera feed switcher (`SCOUT01` / `HAWK01`), and an active aerial geofence overlay.
- In the current implementation, `SCOUT01` is the sole active operational vehicle (`VehicleId = "SCOUT01"`). `HAWK01` is not rendered as an active vehicle card or camera tab; `hawk_geofence` in `GET /map` is retained as a schema-compatible field explicitly labeled `"HAWK01 Flight Envelope (Simulated / Roadmap, Not Integrated)"`.

### Operator Interaction and Route Guardrails

- Mission dispatch, mission completion, replay loading, after-action report generation, event refresh, vehicle status/sensor/observation sync, ROS link status sync, emergency stop, and map-file archival must surface backend errors and confirmation/acknowledgement state instead of appearing successful on failure.
- `SYNC` vehicle telemetry fetches status, raw sensor data, and observations; the raw sensor response remains inspectable as a read-only snapshot.
- Map-file archival accepts `.ply`, `.pcd`, and `.db` files, confirms before upload because canonical backend filenames may be replaced, and states that archival does not update the live XY projection.
- Real hardware mode remains visibly locked (`REAL (LOCKED)`) and non-interactive while `SHADOW_HARDWARE_LOCKOUT=true`.
- The backend ROSBridge status describes backend `/detections_json` ingest only. The live camera and 2D LiDAR map views use direct, read-only browser connections and are never presented as backend actuation links.
