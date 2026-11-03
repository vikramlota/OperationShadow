# Operation Shadow — Genuine Mission Replay Architecture (`docs/MISSION_REPLAY.md`)

**Phase 3 — Workstream C & F Deliverable**  
**Branch**: `phase3-mission-history`

---

## 1. Purpose

`GET /mission/{mission_id}/replay` and `MissionReplayScrubber.tsx` reconstruct the chronological sequence of events, observations, Standing Order applications, LLM triage outcomes, replanning operations, and task transitions for a mission, pairing each timeline entry with the genuine historical World Model state (`MissionMemorySnapshot`) captured at that point in time.

---

## 2. Chronological Reconstruction & Deterministic Tie-Breaking

`MissionService.get_mission_replay(session, mission_id)` queries three persisted SQLite tables for the target `mission_id`:

1. `events` (`EventService.list_events`)
2. `reasoning_traces` (`EventService.list_reasoning_traces`)
3. `mission_snapshots` (`EventService.list_mission_snapshots`)

### Chronological Ordering Rules
All replay timeline items are merged and sorted by a deterministic 3-tuple key:
```python
(entry.timestamp, 0 if entry.entry_type == "event" else 1, entry_id)
```
1. **Primary key**: UTC-normalized timestamp (`entry.timestamp`).
2. **Secondary tie-breaker**: When an `event` and a `reasoning_trace` share the exact same timestamp, the `event` (the causal trigger) precedes the `reasoning_trace` (the resulting ULTRON decision).
3. **Tertiary tie-breaker**: Lexical record ID (`event.id` or `reasoning_trace.id`) guarantees deterministic ordering across identical timestamps and types.

---

## 3. Historical Snapshot Pairing (`world_model_snapshot`)

Each `MissionReplayTimelineEntry` carries `world_model_snapshot: dict[str, Any] | None`, populated by matching persisted `MissionSnapshot` records via:
- `trace_id` (`by_trace_id`)
- `source_event_id` (`by_event_id`)
- `(trigger_type, snapshot_phase)` (`by_trigger_phase`)
- Chronological nearest preceding/succeeding snapshot fallback when a decision trace was created without an explicit snapshot ID.

### 3.1 Reasoning Trace Entries (`entry_type == "reasoning_trace"`)
When historical snapshots exist for the mission, `world_model_snapshot` includes:
- `historical_state_available: True`
- `availability_note`: `"Genuine persisted historical World Model snapshot(s) attached."`
- `before_snapshot_id` and `before_state`: Full `MissionMemorySnapshot` captured immediately **before** the planning/replanning decision.
- `after_snapshot_id` and `after_state`: Full `MissionMemorySnapshot` captured immediately **after** the planning/replanning decision.
- `source_event_id`, `standing_order_id`, `triage_disposition`, `planner_source` (`"gemini"`, `"standing_order"`, `"fallback"`, `"triage"`).
- `validation_passed: bool`, `validation_errors: list[str]`.
- `evidence_ids: list[str]`, `resulting_task_ids: list[str]`.
- `task_changes`:
  - `before_task_ids`
  - `after_task_ids`
  - `added_task_ids`
  - `removed_task_ids`

### 3.2 Event Entries (`entry_type == "event"`)
When historical snapshots exist, `world_model_snapshot` includes:
- `historical_state_available: True`
- `event_category`, `event_type`, `severity` (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, `WARNING`, `INFO`), `source`, `source_mode` (`"simulated"` vs `"physical"`), `evidence_ids`.
- `trigger_snapshot_id` and `trigger_state`: Full `MissionMemorySnapshot` at the moment of the event.
- `before_snapshot_id` / `before_state` and `after_snapshot_id` / `after_state` when the event triggered a paired before/after transition.

### 3.3 Missing Historical State & Legacy Missions
For missions created prior to Phase 3 (or individual entries where no snapshot was recorded):
- `historical_state_available` is explicitly set to `False`.
- `availability_note` states: `"Historical state unavailable: mission was recorded without persistent World Model snapshots (pre-Phase-3 or unrecorded transition)."`
- Current live `WorldModel` state is **never** substituted for historical state.

---

## 4. Dashboard Scrubber UI (`MissionReplayScrubber.tsx`)

The `MissionReplayScrubber` component in `frontend/src/components/MissionReplayScrubber.tsx` renders:
1. **Availability Badge (`data-testid="replay-availability-badge"`)**:
   - Displays `HISTORICAL STATE RECORDED` when `historical_state_available === true`.
   - Displays `HISTORICAL STATE UNAVAILABLE` when `historical_state_available === false` or `world_model_snapshot === null`.
2. **Event & Decision Metadata**:
   - Displays `CATEGORY`, `EVENT TYPE`, `SEVERITY`, `SOURCE`, `SOURCE MODE`, `STANDING ORDER`, `TRIAGE`, `PLANNER`, `VALIDATION`, `EVIDENCE IDS`, and `TASK DIFF` (`added_task_ids` / `removed_task_ids`).
3. **Before & After World Model State Cards**:
   - `data-testid="replay-before-state"`: Displays `snapshot_id`, `mission_status`, `SCOUT01` status/mode/battery/position, active task list, tracked object count, and hazard count before the transition.
   - `data-testid="replay-after-state"`: Displays `snapshot_id`, `mission_status`, `SCOUT01` status/mode/battery/position, active task list, tracked object count, and hazard count after the transition.
