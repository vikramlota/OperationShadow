# Operation Shadow — Phase 4 Product Language Audit (`docs/PHASE4_LANGUAGE_AUDIT.md`)

**Branch**: `phase4-product-language`  
**Phase**: Phase 4 — Tactical C2 Rewording & Product Consistency (Workstream A)  
**Date**: 2026-11-03

---

## 1. Baseline & Phase Report Verification

1. **Git State**:
   - Branch `phase4-product-language` created from `main` at commit `c369728` (`Workstream F: add Phase 3 backend/frontend regression suites, docs/MISSION_REPLAY.md, docs/PHASE3_REPORT.md, and update implementation-plan.md`).
2. **Phase Reports Verified Present**:
   - `docs/PHASE1_REPORT.md` — Present (Phase 1 System Audit, Gemini Deadlines, Signal Honesty & Stabilization).
   - `docs/PHASE2_REPORT.md` — Present (Phase 2 Event-Driven Intelligence: Event Router, Standing Orders & LLM Event Triage).
   - `docs/PHASE3_REPORT.md` — Present (Phase 3 Mission Memory, Genuine Replay & After-Action Intelligence).
3. **Safety & Hardware Lockout Baseline**:
   - `SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_DEFAULT_VEHICLE_MODE=simulated`, `SHADOW_SCOUT_SERIAL_ENABLED=false`, and `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=false` remain the repository defaults across `backend/fastapi/core/config.py` and `.env.example`.

---

## 2. Verification of Specific Known Discrepancies

| # | Known Discrepancy | Source Location | Verification Finding | Phase 4 Disposition |
|---|---|---|---|---|
| **1** | `README.md` describes a local indoor multi-vehicle system coordinating `SCOUT01` and `HAWK01` and a military tactical C2 dashboard. | `README.md:3, 27, 32, 39, 41` | Confirmed: Line 3 states *"Operation Shadow is a local, indoor multi-vehicle mission-control system coordinating two autonomous vehicles (`SCOUT01` ground rover and `HAWK01` indoor drone — simulated / roadmap, not integrated)... and a military tactical command-and-control React + TypeScript dashboard."* | **APPROVED FOR REWORDING** (Workstream C) |
| **2** | `AGENTS.md` describes the product as a local indoor multi-vehicle mission-control system with a professional military tactical C2 aesthetic. | `AGENTS.md:4, 18–21` | Confirmed: Line 4 calls it a *"local, indoor multi-vehicle (`SCOUT01` ground rover and `HAWK01` aerial drone — simulated / roadmap, not integrated) mission-control system"* and Line 19 mandates a *"professional military tactical command-and-control aesthetic"* and *"military green (`#5C744D`)"*. | **APPROVED FOR REWORDING** (Workstream C) |
| **3** | `docs/ui-design-spec.md` describes the product as a field-grade military C2 workstation and includes active HAWK camera, aerial marker, and geofence descriptions. | `docs/ui-design-spec.md:8, 35, 37, 56–77, 102–115` | Confirmed: Line 8 calls it an *"indoor multi-vehicle (`SCOUT01` ground rover, `HAWK01` aerial drone) tactical mission-control console"* that reads *"like a field-grade military command-and-control (C2) workstation"*, and describes active `HAWK01` cards, camera selector tabs (`HAWK01 [CAM-02]`), and E-Stop targets that are not present in the active `SCOUT01`-only UI. | **APPROVED FOR REWORDING** (Workstream C) |
| **4** | `docs/assumptions.md` still describes two vehicle adapters and an original multi-vehicle design without clearly separating original design assumptions from current active `SCOUT01`-only implementation. | `docs/assumptions.md:10, 26, 35, 51–53, 66` | Confirmed: References `docs/ui-design-spec.md` as *"Military tactical command-and-control visual specification"* and mixes historical two-vehicle `MockFleetSimulator` / `VehicleGatewayFastPath` design notes with current implementation status. | **APPROVED FOR REWORDING** (Workstream C) |
| **5** | The frontend types `VehicleId` as `"SCOUT01"` and has a one-vehicle control layout. | `frontend/src/api/client.ts:22`, `frontend/src/App.tsx`, `frontend/src/components/FloorMap.tsx` | Confirmed: `export type VehicleId = "SCOUT01";` and `export type EmergencyStopTarget = "ALL" \| "SCOUT01";`. Only `SCOUT01` is rendered in `App.tsx`, `FloorMap.tsx`, `CameraFeed.tsx`, and `SimulatedModeToggle.tsx`. | **RETAIN AS TECHNICAL CONTRACT** (Ensure docs/UI copy match this active `SCOUT01`-only reality) |
| **6** | The UI retains legacy `.c2-*` CSS classes (`c2-shell`, `c2-panel`, `c2-btn`, `c2-alert-box`, `c2-estop-btn`, `c2-mono`, etc.) and `--accent-military`. | `frontend/src/styles/tactical.css`, `frontend/src/components/*.tsx` | Confirmed: Used across 9 React components and tested via DOM structure. These are internal implementation identifiers. | **RETAIN AS TECHNICAL IDENTIFIER** (Update CSS comments/docs only; never rename `.c2-*` classes or `--accent-military` variable) |

---

## 3. Repository-Wide Terminology Audit & Classification

### 3.1 Classification Categories
- **Cat 1 — User-Facing UI Copy**: Visible labels, headings, aria-labels, badges, and help text in `frontend/src/`.
- **Cat 2 — README & Project Descriptions**: `README.md`, `Makefile`, `pyproject.toml`, `hardware/README.md`.
- **Cat 3 — Contributor / Developer Documentation**: `AGENTS.md`, `docs/implementation-plan.md`, `docs/assumptions.md`, `docs/ui-design-spec.md`, `docs/rosbridge-integration.md`.
- **Cat 4 — Architecture & Technical Specifications**: `docs/Operation Shadow — Backend & Frontend Architecture.md`, `docs/Operation Shadow — ULTRON Agent Architecture.md`.
- **Cat 5 — Historical Requirements**: `docs/Operation Shadow — PRD v2.0.md`, `docs/Operation Shadow — TRD v2.0.md`, and completed Phase 1–3 audit reports (`docs/PHASE1_REPORT.md`, `docs/PHASE2_REPORT.md`, `docs/PHASE3_REPORT.md`, `docs/BUG_AUDIT_AND_OPTIMIZATION_REPORT.md`).
- **Cat 6 — Internal Technical Identifiers**: CSS classes (`.c2-*`), CSS custom properties (`--accent-military`, `--marker-hawk`), schema field names (`hawk_geofence`), enum values, and file paths (`hardware/hawk/`, `backend/ultron_stub/`).
- **Cat 7 — Tests & Fixtures**: `backend/tests/*`, `tests/*`, `frontend/src/__tests__/App.test.tsx`.

### 3.2 Detailed Audit Table

| Term Searched | File & Line(s) | Category | Current Wording | Issue Identified | Proposed Wording / Action | Approved for Phase 4? |
|---|---|---|---|---|---|---|
| `military` / `military tactical` | `README.md:3, 39` | Cat 2 | *"local, indoor multi-vehicle mission-control system coordinating two autonomous vehicles... and a military tactical command-and-control React + TypeScript dashboard"* / *"Military tactical C2 design tokens"* | Overstates active multi-vehicle scope and uses military C2 framing instead of robotics mission control. | Describe as a local robotics mission-control and environmental intelligence platform coordinating `SCOUT01` (with `HAWK01` simulated / roadmap) and a tactical-style robotics operations dashboard. | **YES (Workstream C)** |
| `military` / `command-and-control` | `AGENTS.md:4, 18, 19, 21` | Cat 3 | *"local, indoor multi-vehicle (`SCOUT01`... and `HAWK01`...) mission-control system"*, *"professional military tactical command-and-control aesthetic"*, *"military green (`#5C744D`)"* | Overstates multi-vehicle identity and uses military framing instead of robotics operations console terminology. | Describe as a robotics mission-control platform coordinating `SCOUT01` (with `HAWK01` simulated / roadmap), preserving the dark tactical operations-console visual language and *"operational olive (`#5C744D`)"*. | **YES (Workstream C)** |
| `field-grade` / `military` | `docs/ui-design-spec.md:8, 35` | Cat 3 | *"indoor multi-vehicle (`SCOUT01` ground rover, `HAWK01` aerial drone) tactical mission-control console. Its interface must function and read like a field-grade military command-and-control (C2) workstation"* / *"Accent Secondary (Military Green)"* | Misrepresents the single-vehicle `SCOUT01` dashboard as a field-grade military workstation and two-vehicle active console. | Reword to *"robotics mission-control and operations console"* and *"professional operational command interface"*; describe `--accent-military` as *"Operational Olive (retained variable name `--accent-military` for compatibility)"*. | **YES (Workstream C)** |
| `military` / `command-and-control` | `docs/assumptions.md:10` | Cat 3 | *"`docs/ui-design-spec.md` (Military tactical command-and-control visual specification)"* | Outdated description of UI spec. | Reword to *"Tactical operations-console & mission-control visual specification"* and add explicit Section 1 implementation-status note distinguishing original design assumptions from current `SCOUT01`-centered implementation. | **YES (Workstream C)** |
| `military tactical C2` | `docs/implementation-plan.md:27` | Cat 3 | *"Implement the military tactical C2 design system (`src/styles/tactical.css`)"* | Outdated military C2 phrasing in M5 milestone summary. | Reword to *"tactical operations-console design system (`src/styles/tactical.css`)"* and add implementation-status note on the transition from the original two-vehicle concept to the active `SCOUT01` implementation. | **YES (Workstream C)** |
| `--accent-military` | `frontend/src/styles/tactical.css:1, 54` | Cat 6 | `/* Operation Shadow — Aerospace Tactical Command & Control Design System */` and `--accent-military: var(--accent-active);` | Header comment uses C2 framing; CSS variable `--accent-military` is a technical identifier. | Update header comment to `/* Operation Shadow — Robotics Mission-Control & Tactical Operations Console Design System */`; **keep** `--accent-military` CSS variable name intact. | **Comment: YES (WS D) / Identifier: RETAIN** |
| `multi-vehicle` / `C2` | `Makefile:4, 8, 12`, `pyproject.toml:4` | Cat 2 | `"Operation Shadow — Multi-Vehicle C2 Mission Control"`, `"Start React Tactical C2 Dashboard"`, `"Start FastAPI C2 Server"`, `description = "Operation Shadow Multi-Vehicle Mission Control Workspace..."` | Overstates active multi-vehicle fleet and uses C2 server/dashboard labels. | Update help strings and package description to `"Operation Shadow — Robotics Mission Control & Operations Console"`, `"Start React Operations Dashboard"`, and `"Start FastAPI Mission-Control Server"`. | **YES (Workstream C/E)** |
| `HAWK01` | `docs/Operation Shadow — PRD v2.0.md`, `TRD v2.0.md`, `ULTRON Agent Architecture.md`, `Backend & Frontend Architecture.md` | Cat 4 & Cat 5 | Historical v1.0/v2.0 specifications describing `SCOUT01` + `HAWK01` cross-vehicle architecture. | Versioned historical specs could be misread as describing the current active runtime if not contextualized. | Preserve original specification body and contracts; add a clearly dated **Current Implementation Status Note** at the top of each document linking to `docs/PRODUCT_LANGUAGE.md` and `docs/assumptions.md`. | **YES — Status Note Only (Workstream C)** |
| `HAWK01` | `hardware/README.md:14, 29`, `hardware/hawk/**/*.py`, `configs/vehicles.json`, `configs/geofence.json`, `.env.example` | Cat 2 & Cat 6 | `hardware/README.md` lists `hawk/` without noting it is simulated/roadmap, and says *"against the FastAPI C2 backend"*. | `hardware/README.md` should explicitly label `hardware/hawk/` as simulated / roadmap (not integrated) and use mission-control backend wording. | Update docstrings/comments in `hardware/README.md`, `hardware/hawk/`, `configs/`, and `.env.example` to clearly label `HAWK01` as simulated / roadmap (not integrated) while preserving all files and config keys. | **YES (Workstream E)** |
| `HAWK01` / `hawk_geofence` | `frontend/src/api/client.ts`, `frontend/src/components/FloorMap.tsx`, `backend/fastapi/api/map.py`, `backend/ultron_stub/` | Cat 6 & Cat 7 | `hawk_geofence` field in `GET /map` (`GEOFENCE_HAWK01`, labeled `"HAWK01 Flight Envelope (Simulated / Roadmap, Not Integrated)"`), `StubUltronAgent`, and `MockFleetSimulator`. | Already labeled `"Simulated / Roadmap, Not Integrated"` in `FloorMap.tsx` and `map.py`; `backend/ultron_stub/` docstrings still describe active two-vehicle simulation without noting `HAWK01` is legacy stub/roadmap only. | Keep `hawk_geofence` schema field and `ultron_stub` classes untouched; clarify in `backend/ultron_stub/` docstrings that `HAWK01` is retained for legacy stub/simulator compatibility only. | **Docstrings: YES (WS E) / Contracts: RETAIN** |
| `real hardware` / `autonomous` | `frontend/src/App.tsx`, `frontend/src/components/*.tsx` | Cat 1 | UI badges, panel titles, and notes across `App.tsx`, `MissionPanel.tsx`, `FloorMap.tsx`, `VehicleStatusCard.tsx`, `CameraFeed.tsx`, `SimulatedModeToggle.tsx`, `EmergencyStopButton.tsx`, `PerceptionPanel.tsx`, `ScoutKeyboardControl.tsx`. | Already largely honest after Phases 1–3; minor comments and aria/helper copy still reference `"Tactical C2"` or `"live serial execution"` without clarifying supervised serial dispatch vs simulated default. | Audit and refine operator-facing copy and comments in `frontend/src/` while preserving all test IDs, aria-labels required by existing Vitest tests, `.c2-*` classes, and layout density. | **YES (Workstream D)** |

---

## 4. Summary of Approved Phase 4 Changes vs. Preserved Contracts

1. **Approved for Update**:
   - `docs/PRODUCT_LANGUAGE.md` (new canonical vocabulary & product identity reference — Workstream B).
   - `README.md`, `AGENTS.md`, `docs/ui-design-spec.md`, `docs/assumptions.md`, `docs/implementation-plan.md`, and top-of-file status notes in the 4 versioned architecture/PRD/TRD specs (Workstream C).
   - Operator-facing copy and comments in `frontend/src/` (Workstream D).
   - Docstrings, route descriptions, and configuration comments in `backend/`, `hardware/`, `configs/`, `Makefile`, `pyproject.toml`, and `.env.example` (Workstream E).
2. **Strictly Preserved (Do Not Rename or Alter)**:
   - All REST endpoints, Pydantic schema names/fields (including `hawk_geofence`), WebSocket message types, and SQLite tables.
   - All `.c2-*` CSS class names and `--accent-military` / `--marker-hawk` CSS variables in `frontend/src/styles/tactical.css`.
   - All existing Vitest and Pytest test selectors (`data-testid`, `aria-label` values asserted by tests) and safety/lockout defaults (`SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_DEFAULT_VEHICLE_MODE=simulated`).
