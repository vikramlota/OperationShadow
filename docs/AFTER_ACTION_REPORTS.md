# After-Action Mission Reports (`docs/AFTER_ACTION_REPORTS.md`)

## 1. Overview & Design Principles

In Operation Shadow Phase 3, ULTRON generates a deterministic, evidence-backed **After-Action Mission Report** (`GET /mission/{mission_id}/report`) describing what happened during a mission, what ULTRON decided, what was accomplished, what failed, and what remains unknown.

Core invariants:
1. **Strictly Deterministic (Zero LLM Report Generation)**:
   - Report generation (`MissionService.generate_mission_report()` in `backend/fastapi/services/mission_service.py`) computes every field deterministically from persisted SQLite records (`Mission`, `Task`, `Vehicle`, `Observation`, `Event`, `ReasoningTrace`, and `MissionSnapshot`).
   - It never invokes Gemini (`llm_used_for_report: false`, `report_deterministic: true`) and works 100% offline.
2. **Separate from `MissionPlan`**:
   - `MissionReportResponse` (`backend/fastapi/schemas/entities.py`) is an independent read-only API schema; `MissionPlan` (`ultron/schemas/mission.py`) is untouched.
3. **Read-Only & Idempotent**:
   - Inspecting a report never dispatches commands to a vehicle, never modifies mission status, and produces consistent factual output on repeated calls.
4. **Epistemic & Provenance Honesty**:
   - **Simulated vs. Physical Execution**: Clearly labels simulated execution (`is_simulated_execution: true`, `vehicle_mode: "simulated"`). Tasks completed via `POST /mission/{id}/complete` or while `SHADOW_HARDWARE_LOCKOUT=true` are recorded as `SIMULATED_COMPLETED`, never `PHYSICAL_EXECUTION_CONFIRMED`.
   - **Execution Stage Separation**: Distinguishes `PLANNED`, `ASSIGNED`, `COMMAND_ACCEPTED`, `COMMAND_DISPATCHED`, `VEHICLE_ACKNOWLEDGED`, `PHYSICAL_EXECUTION_CONFIRMED`, and `SIMULATED_COMPLETED`. A planned or assigned task is never reported as physically executed.
   - **Coordinate Frame Preservation**: Camera-relative detections (`oak_rgb_camera_optical_frame`, `oak_parent_frame`) and unconfirmed `map` detections are never converted into global floor-map positions (`map_position_x: null`, `map_position_y: null`).
   - **Missing Evidence**: Missing observations or unconfigured sensors (such as `lidar` `UNAVAILABLE`) are reported explicitly as gaps in `unresolved_items` and `coverage_note`—never as proof that an area was clear or hazard-free.

---

## 2. Report Structure (`MissionReportResponse`)

| Section | Schema Class | Key Fields |
| --- | --- | --- |
| **Top-Level Metadata** | `MissionReportResponse` | `mission_id`, `generated_at`, `report_deterministic` (`true`), `llm_used_for_report` (`false`), `historical_snapshots_count`, `historical_state_available`, `summary_text`, `markdown_export`. |
| **1. Mission Overview** | `MissionReportOverview` | `mission_id`, `objective`, `created_at`, `completed_at`, `duration_seconds` (calculated only when timestamps support it), `final_status`, `vehicle_mode`, `is_simulated_execution`, `hardware_lockout_active`, `execution_provenance`. |
| **2. Task Summary** | `MissionReportTaskSummary` | `total_tasks`, `completed_tasks`, `pending_tasks` (all incomplete tasks: `PENDING` + `ASSIGNED` + `IN_PROGRESS`), `assigned_tasks`, `in_progress_tasks`, `failed_tasks`, `cancelled_tasks`, and itemized `tasks` (`MissionReportTaskRecord` with `highest_evidence_stage`, `is_simulated`, `source_trace_ids`, `provenance_note`). |
| **3. Environmental Findings** | `MissionReportEnvironmentalFindings` | `detected_objects`, `identified_targets`, `reported_hazards`, `obstacles`, `sensor_observations`, `evidence_references`, `coordinate_frame_limitations`, and `coverage_note`. |
| **4. ULTRON Decision History** | `MissionReportDecisionHistory` | `important_events`, `standing_orders_applied`, `llm_triage_outcomes`, `replanning_operations`, `rejected_decisions`, `accepted_plans`, and `human_escalation_events`. |
| **5. Vehicle Summary** | `MissionReportVehicleSummary` | `vehicle_id` (`SCOUT01`), `vehicle_type` (`ground`), `mode`, `final_status`, `final_connection`, `connection_history`, `battery_observations` (`min_battery`, `max_battery`, `final_battery`, `sample_count`, `critical_low_battery_observed`), `task_participation_ids`, `communication_failures`, and `operational_limitations` (`HAWK01` simulated/roadmap, hardware lockout, unconfirmed LiDAR/TF). |
| **6. Safety Summary** | `MissionReportSafetySummary` | `emergency_stop_events`, `safety_validation_failures`, `critical_faults`, `stop_dispatch_acknowledged_status`, and `unresolved_safety_issues`. |
| **7. Unresolved Items** | `MissionReportUnresolvedItems` | `incomplete_tasks`, `missing_observations`, `stale_telemetry_or_observations`, `conflicting_evidence`, `unverified_sensor_signals`, and `unknown_outcomes`. |

---

## 3. API Contract & Export Behavior

### Endpoint
```http
GET /mission/{mission_id}/report
```
Optional query parameter:
- `format=json` (default): Returns HTTP `200 OK` with `application/json` matching `MissionReportResponse` (which includes both the structured JSON sections and `markdown_export: str`).
- `format=markdown`: Returns HTTP `200 OK` with `text/markdown; charset=utf-8` containing the formatted Markdown report.

### Error Responses
- `404 Not Found` (`RESOURCE_NOT_FOUND`): Returned when `mission_id` does not exist in SQLite `missions`.

### Supported Mission Lifecycle States
- **Completed Missions (`COMPLETED`)**: Full duration, completed task statistics, and explicit `SIMULATED_COMPLETED` provenance when completed in simulation.
- **Active / Incomplete Missions (`ACTIVE`, `IN_PROGRESS`, `PENDING`)**: Reports current progress, lists all `ASSIGNED`/`IN_PROGRESS`/`PENDING` tasks under `unresolved_items.incomplete_tasks`, and leaves `completed_at: null`.
- **Degraded / Failed / Cancelled Missions (`DEGRADED`, `FAILED`, `ABORTED`)**: Captures validation failures, `CANCELLED`/`FAILED` tasks, and unresolved safety issues.
