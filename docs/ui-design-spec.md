# Operation Shadow — UI & Tactical Design Specification

**Scope:** Visual design system, color tokens, typography, component styling, layout architecture, and visual hierarchy for the Operation Shadow React mission-control console (`dashboard/react/`).
**Implementation Phase:** Scheduled for the Frontend Milestones (Milestones 6 & 7) per `docs/implementation-plan.md`.

## 1. Design Philosophy & Aesthetic Direction

Operation Shadow is an indoor multi-vehicle (`SCOUT01` ground rover, `HAWK01` aerial drone) tactical mission-control console. Its interface must function and read like a field-grade military command-and-control (C2) workstation:
- **High Information Density, Zero Visual Fluff**: Every pixel serves situational awareness, telemetry inspection, or rapid operator intervention.
- **Low-Light Operational Ergonomics**: Matte near-black and dark charcoal surfaces prevent eye fatigue and make tactical vectors, amber caution states, and the red emergency-stop control immediately legible.
- **Deterministic Readability**: Fixed-width tabular numerals ensure rapidly updating telemetry (`200–500ms` heartbeat cadence) never causes layout jitter.

### Explicitly Prohibited Anti-Patterns
- **No AI/SaaS clichés**: No purple, violet, or cyan-to-magenta gradients; no "AI sparkle" icons, chat bubbles, or decorative robot illustrations.
- **No consumer card bloat**: No oversized border-radiuses (`> 4px`), heavy drop shadows, floating glassmorphic panels (`backdrop-filter: blur`), or spacious marketing whitespace.
- **No neon cyberpunk tropes**: Avoid glowing text-shadows, saturated laser greens, or decorative HUD animations that do not represent real backend state.

## 2. Color Palette & CSS Custom Properties

All colors are defined as semantic CSS variables in the root stylesheet during the frontend milestone:

| Token Category | CSS Variable | Hex Value | Operational Usage |
| --- | --- | --- | --- |
| **Canvas Background** | `--bg-canvas` | `#0A0D0C` | Root viewport background, tactical map void |
| **Primary Panel Surface** | `--bg-panel` | `#111614` | Standard operational module background (`MissionPanel`, `EventLog`, `ReasoningTracePanel`) |
| **Elevated / Header Surface** | `--bg-panel-header` | `#171E1B` | Module header bars, table headers, scrub bar track |
| **Recessed / Input Surface** | `--bg-recessed` | `#0D110F` | Command inputs, log viewports, camera viewport letterbox |
| **Primary Border** | `--border-subtle` | `#26302B` | `1px solid` panel framing and internal dividers |
| **Active / Tactical Border** | `--border-tactical` | `#4A5D3F` | Active panel focus, selected vehicle card border |
| **Tactical Grid Hairline** | `--grid-line` | `#1A221E` | SVG floor-plan coordinate grid and subtle background rules |
| **Primary Text** | `--text-primary` | `#E6E8E1` | Warm off-white primary labels, mission objectives, active readouts |
| **Secondary Text** | `--text-secondary` | `#9CA59B` | Muted sage-gray field labels, secondary descriptions |
| **Muted / Disabled Text** | `--text-muted` | `#636E64` | Inactive controls, historical timestamps, axis ticks |
| **Accent Primary (Olive Drab)** | `--accent-olive` | `#4A5D3F` | Primary tactical controls, active tab backgrounds, button fills |
| **Accent Secondary (Military Green)** | `--accent-military` | `#5C744D` | Nominal status badges (`ONLINE`, `ACTIVE`, `SIMULATED`), interactive hover borders |
| **Accent Highlight (Desaturated Sage)** | `--accent-sage` | `#8AA37B` | Active waypoint markers, `SCOUT01` vector path, selected timeline node |
| **Tactical Aerial Marker** | `--marker-hawk` | `#7A9A95` | Desaturated tactical slate-teal for `HAWK01` aerial position & altitude indicator |
| **Warning / Degraded (Amber)** | `--status-warning` | `#D98A2B` | Low battery, missed heartbeat (`UNKNOWN` state), simulated-mode lockout banner, HAWK geofence boundary |
| **Critical / Emergency Stop (Red)** | `--status-critical` | `#C93535` | Emergency Stop control fill, connection loss (`DISCONNECTED` fault), hazard markers |
| **Emergency Stop Active/Hover** | `--status-critical-bright` | `#E03E3E` | High-contrast border and active press state for `EmergencyStopButton` |

## 3. Typography & Telemetry Formatting

| Role | Font Stack | Size / Weight / Tracking | Application |
| --- | --- | --- | --- |
| **UI & Panel Headers** | `'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif` | `11px–13px`, `600–700`, uppercase, `letter-spacing: 0.08em` | Panel titles (`FLOOR MAP // SECTOR 01`), section headers, button labels |
| **Body & Rationale Copy** | `'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif` | `13px`, `400–500`, `line-height: 1.4` | Mission objectives, ULTRON reasoning trace rationale text, event descriptions |
| **Telemetry & Tactical Data (Monospace)** | `'JetBrains Mono', 'SFMono-Regular', 'Menlo', 'Consolas', monospace` | `12px–14px`, `500`, `font-variant-numeric: tabular-nums` | Vehicle IDs (`SCOUT01`, `HAWK01`), coordinates (`X: 04.20m Y: 08.15m ALT: 01.50m`), battery `%`, timestamps (`23:55:18.240Z`), mission/task IDs (`M001`, `T004`) |

### Formatting Rules
- All numeric telemetry (`battery`, `position_x`, `position_y`, `position_alt`, latency counters) must use `font-variant-numeric: tabular-nums` and fixed decimal precision so digits never shift horizontally during `200–500ms` WebSocket pushes.
- Timestamps in `EventLog` and `ReasoningTracePanel` display in compact 24-hour UTC/local tactical format (`HH:MM:SS.mmm`).
- Vehicle identifiers (`SCOUT01`, `HAWK01`) and status codes (`IDLE`, `EXECUTING`, `UNKNOWN`, `STOPPED`) are always rendered in uppercase monospace.

## 4. Layout Architecture & Visual Hierarchy

The console is designed for a `1440×900` to `1920×1080` desktop operator screen as a fixed-viewport, non-scrolling command grid (`100vh`, `display: grid`, `gap: 6px`, `padding: 6px`):

```text
+--------------------------------------------------------------------------------------------------+
| TOP COMMAND BAR (48px fixed height)                                                              |
| [OP-SHADOW // C2] [MISSION: M001 - ACTIVE] [WS: LINKED] [LOCKOUT: SIMULATED ONLY] [ E-STOP ALL ] |
+-------------------------------+--------------------------------------+---------------------------+
| LEFT COLUMN (280px–320px)     | CENTER TACTICAL VIEWPORT (1fr)       | RIGHT COLUMN (340px–380px)|
|                               |                                      |                           |
| +---------------------------+ | +----------------------------------+ | +-----------------------+ |
| | MISSION CONTROL PANEL     | | | INDOOR SVG FLOOR MAP (GET /map)  | | | CAMERA FEED (MJPEG)   | |
| | Objective input, dispatch | | | - Room walls & coordinate grid   | | | - SCOUT01 / HAWK01    | |
| | & active task list        | | | - HAWK geofence (amber dashed)   | | |   selector tabs       | |
| +---------------------------+ | | - Fixed waypoints                | | | - Direct <img> stream | |
| | VEHICLE CARD: SCOUT01     | | | - Live SCOUT01 & HAWK01 markers  | | |   or Simulated SVG    | |
| | [GROUND] [SIM] [BAT 94%]  | | | - Target & hazard overlays       | | +-----------------------+ |
| | X: 02.40 Y: 05.10 ALT:0.0 | | +----------------------------------+ | | ULTRON REASONING      | |
| +---------------------------+ |                                      | | TRACE PANEL           | |
| | VEHICLE CARD: HAWK01      | | +----------------------------------+ | | Trigger -> Rationale  | |
| | [AERIAL] [SIM] [BAT 88%]  | | | MISSION REPLAY SCRUBBER          | | | -> Resulting Task IDs | |
| | X: 06.10 Y: 03.80 ALT:1.8 | | | Post-hoc timeline & World Model  | | +-----------------------+ |
| +---------------------------+ | | plan_before / plan_after view    | | | CHRONOLOGICAL         | |
| | SIMULATED MODE TOGGLE     | | +----------------------------------+ | | EVENT LOG             | |
| | Per-vehicle mode & lock   | |                                      | | [HH:MM:SS] [CAT] Msg  | |
| +---------------------------+ |                                      | +-----------------------+ |
+-------------------------------+--------------------------------------+---------------------------+
```

### Visual Hierarchy Priorities
1. **Priority 1 — Safety & Emergency Stop (`EmergencyStopButton`)**: Pinned to the top-right of the Top Command Bar at all times. High-contrast crimson fill (`#C93535`), bold uppercase label, thin hazard border, and one-click targeting for `ALL`, `SCOUT01`, or `HAWK01`. Nothing may obscure or push this control off-screen.
2. **Priority 2 — Spatial Situational Awareness (`FloorMap` & `VehicleStatusCard`)**: Center-stage SVG indoor map with real-time `SCOUT01` and `HAWK01` position vectors, paired with compact telemetry cards on the left rail showing battery, connection health, mode (`SIMULATED` / `REAL`), and `x/y/alt` readouts.
3. **Priority 3 — Visual Confirmation & Agent Transparency (`CameraFeed`, `ReasoningTracePanel`, `EventLog`)**: Right-hand operational rail stacking the direct MJPEG camera viewport, ULTRON's decision trace (`trigger`, `rationale`, `resulting_task_ids`), and chronological system events.
4. **Priority 4 — Post-Mission Analysis (`MissionReplayScrubber`)**: Docked below the tactical map; remains compact in standby during live execution and expands to scrub ordered events and World Model snapshots (`plan_before` / `plan_after`) once a mission completes.

## 5. Component Styling Specifications

- **Operational Panels**:
  - Background: `--bg-panel` (`#111614`), border: `1px solid var(--border-subtle)` (`#26302B`), border-radius: `2px`, box-shadow: `none`.
  - Header bar: `28px` height, `--bg-panel-header` (`#171E1B`), bottom border `1px solid var(--border-subtle)`, left accent bar `3px solid var(--accent-olive)` (`#4A5D3F`), uppercase title with right-aligned status metadata.
- **SVG Floor Map (`FloorMap.tsx`)**:
  - Background: `--bg-canvas` (`#0A0D0C`) with a subtle SVG `<pattern>` coordinate grid (`--grid-line`, `#1A221E`) and numeric meter/unit ruler ticks along the top and left edges.
  - Room boundaries rendered in crisp `1.5px` strokes (`#5C744D`); HAWK geofence rendered in `1.5px` dashed amber (`#D98A2B`); `SCOUT01` rendered as a tactical diamond/chevron with sage callout label; `HAWK01` rendered as an aerial crosshair/reticle with altitude readout.
- **Vehicle Status Cards (`VehicleStatusCard.tsx`)**:
  - Compact 2-column key-value telemetry grid using monospace numerals, a segmented horizontal battery bar (green `> 40%`, amber `20%–40%`, red `< 20%`), and explicit badge chips for `MODE: SIMULATED` and `LINK: CONNECTED | UNKNOWN | DISCONNECTED`.
- **Camera Feed (`CameraFeed.tsx`)**:
  - 16:9 or 4:3 recessed viewport (`--bg-recessed`) with subtle corner reticle ticks, a top selector bar to switch between `SCOUT01 [CAM-01]` and `HAWK01 [CAM-02]`, and an explicit tactical test-pattern SVG placeholder when operating in `simulated` mode without a live vehicle HTTP stream.
- **Emergency Stop Control (`EmergencyStopButton.tsx`)**:
  - Distinct rectangular tactical switch (`background: #C93535`, `border: 1px solid #FF6B6B`, `color: #FFFFFF`, `font-family: monospace`, `font-weight: 700`) with immediate dispatch feedback (`< 100ms` fast path) and individual quick-stop triggers for `ALL`, `SCOUT01`, and `HAWK01`.
