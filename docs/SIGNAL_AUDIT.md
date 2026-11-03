# Operation Shadow — End-to-End Signal Audit (`docs/SIGNAL_AUDIT.md`)

## 1. Verified Code Facts

1. **Vehicle Telemetry Validator (`backend/fastapi/websocket/vehicle_gateway.py:119-160`)**:
   - `validate_inbound_telemetry_frame(vid, tel_data)` strictly requires:
     - `status` (`str` in `{"IDLE", "ACTIVE", "EXECUTING", "UNKNOWN", "EMERGENCY_STOPPED", "FAULT"}`)
     - `battery` (finite `float` in `[0.0, 100.0]`)
     - `position_x` (finite `float`)
     - `position_y` (finite `float`)
     - `timestamp` (ISO-8601 string, `datetime`, or positive finite Unix epoch float)
   - Optionally accepts `position_alt` (must be `0.0` for `SCOUT01`) and `raw_sensors` (`dict[str, Any]`).
   - Any `raw_sensors` dict is stored on `TelemetryPayloadStub` and merged into `GET /vehicle/SCOUT01/sensors` (`backend/fastapi/services/vehicle_service.py:362-378`), which `frontend/src/components/VehicleStatusCard.tsx:249-265` renders inside `<details>SENSOR SNAPSHOT</details>`.
   - Note: `VehicleService.ingest_telemetry` (`backend/fastapi/services/vehicle_service.py:446-520`) updates SQLite, the ULTRON `WorldModel`, and the `/ws/dashboard` telemetry broadcast, but does **not** overwrite the adapter's `_latest_telemetry` object; `GET /vehicle/SCOUT01/sensors` reads `latest_telem.raw_sensors` from the active `VehicleAdapterPort`.
2. **Rover Pi Sensor Fusion Packet Mismatch (`hardware/scout/sensors/fusion.py:13-37`)**:
   - `SensorFusion.fuse_telemetry(...)` constructs:
     ```json
     {
       "robot_id": "SCOUT01",
       "vehicle_type": "ground",
       "status": "EXPLORING",
       "battery": 95.0,
       "position": {"x": 1.0, "y": 2.0},
       "heading": 0.0,
       "temperature": 25.0,
       "gas_level": "NORMAL",
       "front_distance_cm": 120.0,
       "connection": "ONLINE"
     }
     ```
   - This packet **fails** `validate_inbound_telemetry_frame` in `vehicle_gateway.py` for four reasons:
     1. Default `status="EXPLORING"` is not in `VehicleStatusEnum` (`IDLE`, `ACTIVE`, `EXECUTING`, `UNKNOWN`, `EMERGENCY_STOPPED`, `FAULT`).
     2. Coordinates are nested under `"position": {"x": ..., "y": ...}` instead of top-level `position_x` and `position_y`.
     3. Required `timestamp` field is completely absent.
     4. `temperature`, `gas_level`, `front_distance_cm`, and `heading` are top-level keys rather than inside `raw_sensors`, so even if the packet passed validation they would be dropped.
   - Furthermore, `hardware/scout/main.py:10-24` instantiates `SensorFusion`, `ScoutWebSocketClient`, and `ESP32SerialBridge` without an active async loop that reads serial frames or calls `ws_client.send(...)`.
3. **ROSBridge Perception Ingest (`backend/fastapi/services/rosbridge_client.py` & `rosbridge_adapter.py`)**:
   - Both `RosBridgeAdapter` (`backend/fastapi/services/rosbridge_adapter.py:155-182`, used by `PerceptionService`) and `RosBridgeClient` (`backend/fastapi/services/rosbridge_client.py:101-118`, started when `SHADOW_ROSBRIDGE_ENABLED=true`) subscribe **exclusively** to `/detections_json` (`std_msgs/msg/String`).
   - Neither backend client subscribes to `/scout/odom`, `/scout/battery`, `/cloud_registered`, or `/path`.
4. **Simulated Mode (`backend/ultron_stub/simulator.py:394-436`)**:
   - `MockVehicleAdapter.emit_heartbeat()` emits `TelemetryPayloadStub` with `vehicle_id`, `battery`, `position_x`, `position_y`, `position_alt`, `status`, `timestamp`, `sequence`, and `raw_sensors=None`.
   - `VehicleService.get_vehicle_sensors` (`backend/fastapi/services/vehicle_service.py:372-377`) populates `raw_sensors` with `{"is_simulated": True, "vehicle_type": "ground", "active_task_id": ...}`.

---

## 2. Signal Audit Matrix

| Signal | Producer (`file:line`) | Transport | Backend Ingest Point | Reaches Backend by Code? (`YES`/`NO`/`UNKNOWN` + Evidence) | Emitted by Simulator? | Real-Rover Result *(Human to fill)* | Trigger Candidate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **Camera Detections** (`objects`: `id`, `class`, `score`, `position`, `range_m`, `frame`, `stamp`) | Rover ROS 2 node publishing `/detections_json` (`std_msgs/msg/String`); simulated via `PerceptionService.ingest_detection_frame` (`backend/fastapi/services/perception_service.py:424-527`) | ROSBridge WebSocket `ws://<SHADOW_ROS_HOST>:9090` (`RosBridgeAdapter` in `rosbridge_adapter.py:293-336` & `RosBridgeClient` in `rosbridge_client.py:224-252`) | `PerceptionService.handle_raw_ros_message` (`perception_service.py:389-422`) and `process_rosbridge_detection_payload` (`rosbridge_client.py:255-483`) → `UltronLangGraphAgent.ingest_detection_frame_into_world_model` (`ultron_agent_adapter.py:553-702`) | **YES** — Full end-to-end validation, freshness tracking (`is_stale`), `WorldModel` object tracking (`TrackedObject`), `/ws/dashboard` `detections` broadcast, and `GET /vehicle/SCOUT01/detections` are wired and verified by tests. Requires `SHADOW_ROS_ENABLED=true`, `SHADOW_ROS_HOST=<SCOUT_IP>`, and `SHADOW_ROS_VEHICLE_ID=SCOUT01`. | **YES (via `PerceptionService` / `UltronLangGraphAgent` injection)**; `MockVehicleAdapter` does not auto-emit camera frames on tick unless injected. | | **REAL TRIGGER #1** (Hazard/obstacle/target class detected on `/detections_json`, e.g., `person`, `box`, `chair`, `fire`) |
| **Battery** (`battery` %) | Sim: `MockVehicleAdapter.emit_heartbeat` (`backend/ultron_stub/simulator.py:419-428`).<br>Rover Pi: `SensorFusion.fuse_telemetry` (`hardware/scout/sensors/fusion.py:19,26`). | Vehicle WebSocket `WS /ws/vehicle` (`websocket_client.py:19-25`) or dead-reckoning pose sync (`mission_service.py:310-321`) | `validate_inbound_telemetry_frame` (`vehicle_gateway.py:132`) → `VehicleService.ingest_telemetry` (`vehicle_service.py:446-520`) → `GET /vehicle/SCOUT01/status` & `/ws/dashboard` `telemetry` | **NO from `hardware/scout/` as written (YES over `/ws/vehicle` if packet shape is fixed)** — `vehicle_gateway.py` accepts `battery`, but `fusion.py` omits `timestamp`, uses nested `position`, and sends `status="EXPLORING"`, so `validate_inbound_telemetry_frame` rejects it. Also no ROSBridge subscriber exists for `/scout/battery`. | **YES** (`100.0%`, drains by `0.1%` per active `step()` in `simulator.py:446`). | | **SIMULATED TRIGGER** (unless `/ws/vehicle` client or ROS `/scout/battery` subscriber is wired; low-battery `< 20%` trigger already exists in `ultron/replanner/triggers.py:16-24`) |
| **Connection State & Heartbeat** (`connection`, `last_heartbeat`, `is_stale`) | Sim: `MockVehicleAdapter.connect` / `emit_heartbeat` (`simulator.py:263-271,394-436`).<br>ROSBridge: `RosBridgeAdapter._on_ros_connected_event` (`rosbridge_adapter.py:255-291`). | `WS /ws/vehicle` connect/disconnect + heartbeat timeout monitor (`vehicle_service.py:522-566`), and ROSBridge WS state (`perception_service.py:372-387`) | `VehicleGateway.handle_vehicle_session` (`vehicle_gateway.py:456-461,551-554`), `VehicleService.evaluate_heartbeats` (`vehicle_service.py:522-566`), and `PerceptionService.handle_ros_connection_state_change` (`perception_service.py:372-387`) | **YES** — Both `/ws/vehicle` session lifecycle (`CONNECTED` → `UNKNOWN` after `1500ms` timeout → `DISCONNECTED`) and ROSBridge connection state (`DISABLED`/`DISCONNECTED`/`CONNECTING`/`CONNECTED`/`RECONNECTING` + `is_stale` after `2.0s`) reach the backend and `/ws/dashboard`. | **YES** (`CONNECTED` on startup seed, `is_stale` computed against `SHADOW_HEARTBEAT_TIMEOUT_MS=1500`). | | **REAL TRIGGER #2** (ROSBridge perception link state / detection stream staleness `is_stale=true` or vehicle heartbeat `UNKNOWN` timeout) |
| **ToF / Ultrasonic Distance** (`front_distance_cm` / `distanceCm`) | ESP32: `SensorReader::readAll` (`hardware/firmware/esp32/sensors/sensor_reader.h:9-13`).<br>Pi UART: `ESP32SerialBridge.parse_sensor_frame` (`hardware/scout/communication/serial_bridge.py:19-28`).<br>Pi Fusion: `SensorFusion.fuse_telemetry` (`hardware/scout/sensors/fusion.py:22,31`). | ESP32 UART CSV (`distance,temp,gas`) → Pi `SensorFusion` → `WS /ws/vehicle` | None (`validate_inbound_telemetry_frame` in `vehicle_gateway.py:119-160` drops unknown top-level keys and rejects `fusion.py` packet) | **NO** — Blocked by (1) `hardware/scout/main.py` not running a serial-to-WS loop, (2) `fusion.py` packet failing `validate_inbound_telemetry_frame`, and (3) `front_distance_cm` not being placed inside `raw_sensors` or persisted by `ingest_telemetry`. | **NO** (`raw_sensors` in `simulator.py` does not include `front_distance_cm`). | | **SIMULATED TRIGGER** (Obstacle proximity `< 30 cm`; simulated in Phase 1/2 via `raw_sensors` or `/detections_json` `range_m`) |
| **Temperature** (`temperature` / `temp_c` / `temperatureC`) | ESP32: `SensorReader::readAll` (`sensor_reader.h:10`).<br>Pi UART: `ESP32SerialBridge.parse_sensor_frame` (`serial_bridge.py:25`).<br>Pi Fusion: `SensorFusion.fuse_telemetry` (`fusion.py:20,29`). | ESP32 UART CSV → Pi `SensorFusion` → `WS /ws/vehicle` | None (`validate_inbound_telemetry_frame` in `vehicle_gateway.py:119-160`) | **NO** — Same 3 blockers as ToF/ultrasonic distance (`main.py` loop missing, `fusion.py` shape mismatch, and `temperature` not nested in `raw_sensors`). | **NO** | | **SIMULATED TRIGGER** (Thermal anomaly `> 45 °C` trigger) |
| **Gas Sensor** (`gas_level` / `gas_raw` / `gasRaw`) | ESP32: `SensorReader::readAll` (`sensor_reader.h:11`).<br>Pi UART: `ESP32SerialBridge.parse_sensor_frame` (`serial_bridge.py:26`).<br>Pi Fusion: `SensorFusion.fuse_telemetry` (`fusion.py:21,30`). | ESP32 UART CSV → Pi `SensorFusion` → `WS /ws/vehicle` | None (`validate_inbound_telemetry_frame` in `vehicle_gateway.py:119-160`) | **NO** — Same 3 blockers as ToF/ultrasonic distance (`main.py` loop missing, `fusion.py` shape mismatch, and `gas_level` not nested in `raw_sensors`). | **NO** | | **SIMULATED TRIGGER** (Gas leak / hazardous atmosphere trigger) |
| **Pose** (`position_x`, `position_y`, `position_alt`, `heading`) | Sim: `MockVehicleAdapter` (`simulator.py:421-423`) & dead-reckoning in `MissionService._broadcast_scout_pose` (`mission_service.py:283-333`).<br>Rover Pi: `SensorFusion.fuse_telemetry` (`fusion.py:16-18,27-28`).<br>Rover SLAM: `/path` (`nav_msgs/msg/Path`). | `WS /ws/vehicle` (`position_x`, `position_y`, `position_alt`), dead-reckoning in `mission_service.py`, or frontend-direct ROSBridge `/path` (`LidarScanView.tsx:249-269`) | `validate_inbound_telemetry_frame` (`vehicle_gateway.py:133-139`) → `VehicleService.ingest_telemetry` (`vehicle_service.py:446-520`). Note: `/path` is subscribed directly by the React browser in `LidarScanView.tsx`, **not** by the FastAPI backend. | **PARTIAL (`YES` for simulated/dead-reckoned `position_x`/`position_y`; `NO` from physical rover)** — `fusion.py` nests `{"position": {"x", "y"}}` (rejected by `vehicle_gateway.py`) and top-level `heading` is ignored; backend `rosbridge_adapter.py` has only an unimplemented `OptionalTopicExtensionPort` stub (`rosbridge_adapter.py:92-108`) for `/oak/vio/odometry`. | **YES** (`position_x`, `position_y`, `position_alt=0.0` emitted in `simulator.py` and dead-reckoned during `execute_mission_on_rover`). | | **REAL TRIGGER #3 (via SLAM `/path` or `/ws/vehicle` once reshaped) / SIMULATED TRIGGER** (Geofence / zone arrival trigger) |
| **LiDAR** (`PointCloud2` / `LaserScan`) | Rover SLAM: `/cloud_registered` (`sensor_msgs/msg/PointCloud2`).<br>ULTRON adapter stub: `LidarIngestionAdapter` (`ultron/vision/lidar.py:18-175`, disabled by `SHADOW_LIDAR_ENABLED=false`). | Frontend subscribes directly to `ws://<ROS_HOST>:9090` topic `/cloud_registered` (`frontend/src/components/LidarScanView.tsx:194-247`). Backend has **no** active ROSBridge subscription for LiDAR. | `LidarIngestionAdapter.ingest_dict_frame` (`ultron/vision/lidar.py:59-175`) exists in ULTRON, but no ROSBridge client in `backend/fastapi/services/` is wired to call it. | **NO (Reaches Frontend UI only, not Backend)** — `LidarScanView.tsx` renders `/cloud_registered` directly in the browser canvas, while backend `PerceptionService` reports `lidar: UNAVAILABLE` (`perception_service.py:293-310`). | **NO in backend** (`status: "UNAVAILABLE"`); **YES in frontend** via `LOAD DEMO 2D SLAM MAP` button (`LidarScanView.tsx:131-162`). | | **SIMULATED TRIGGER** in backend (Unverified free-space / corridor obstruction trigger) |

---

## 3. Smallest Code Changes Required to Carry Blocked Signals (Do Not Implement in Phase 1)

1. **Reshape `SensorFusion.fuse_telemetry` (`hardware/scout/sensors/fusion.py`) to match `validate_inbound_telemetry_frame`**:
   - Replace nested `"position": {"x": ..., "y": ...}` with top-level `"position_x"` and `"position_y"`.
   - Map `"EXPLORING"` to `"ACTIVE"` or `"EXECUTING"` (valid `VehicleStatusEnum` values).
   - Add `"timestamp": datetime.now(timezone.utc).isoformat()`.
   - Nest `"temperature"`, `"gas_level"`, `"front_distance_cm"`, and `"heading"` inside `"raw_sensors": {...}`:
     ```python
     return {
         "status": "ACTIVE" if status == "EXPLORING" else status,
         "battery": round(battery, 1),
         "position_x": round(position_x, 2),
         "position_y": round(position_y, 2),
         "position_alt": 0.0,
         "timestamp": datetime.now(timezone.utc).isoformat(),
         "raw_sensors": {
             "heading": round(heading, 1),
             "temperature": round(temperature, 1),
             "gas_level": gas_level,
             "front_distance_cm": round(front_distance_cm, 1),
         },
     }
     ```
2. **Preserve `telemetry.raw_sensors` in `VehicleService.ingest_telemetry` (`backend/fastapi/services/vehicle_service.py:446-520`)**:
   - Store the latest accepted `telemetry.raw_sensors` dict per `vehicle_id` in `VehicleService._latest_raw_sensors[vehicle_id]` and merge it in `get_vehicle_sensors` (`vehicle_service.py:372-377`), because `ingest_telemetry` currently updates SQLite and `WorldModel` without updating `adapter._latest_telemetry`.
3. **Wire an async telemetry loop in `hardware/scout/main.py` (or a ROSBridge subscriber for `/scout/odom` and `/scout/battery`)**:
   - In `hardware/scout/main.py`, read lines from `ESP32SerialBridge`, pass them through `SensorFusion.fuse_telemetry`, and send them via `ScoutWebSocketClient` every `250ms`—or add read-only subscriptions in `RosBridgeAdapter` for `/scout/odom` and `/scout/battery` once topic names/types are confirmed on the rover.

---

## 4. Recommended Phase 2 Trigger Selection

Based on what reaches the backend today:
- **Real Trigger Candidates (2–3)**:
  1. **Camera Object / Obstacle / Hazard Detection (`/detections_json`)**: Live end-to-end through `RosBridgeAdapter` → `PerceptionService` → `UltronLangGraphAgent`. Can trigger standing orders on class (`person`, `chair`, `box`, `fire`, `smoke`) and camera-relative range (`range_m < 1.5m`).
  2. **Camera Detection Stream Freshness / Link Loss (`is_stale` / `ros_connection_state`)**: Live end-to-end through `PerceptionService` and `GET /vehicle/SCOUT01/detections` (`stale_after_seconds = 2.0s`).
  3. **Vehicle Heartbeat Timeout (`connection == "UNKNOWN"` / `is_stale == True`)**: Live end-to-end through `VehicleService.evaluate_heartbeats` (`SHADOW_HEARTBEAT_TIMEOUT_MS = 1500ms`).
- **Labeled Simulated Triggers (Everything Else)**:
  - **Battery Critical (`< 20%`)**: Simulated via `VehicleService` / `MockVehicleAdapter` (or `POST /vehicle/SCOUT01/simulate`).
  - **Temperature Anomaly (`temperature > 45°C`)**: Labeled simulated trigger injected via `raw_sensors` / event router.
  - **Gas Leak (`gas_level == "HIGH"`)**: Labeled simulated trigger injected via `raw_sensors` / event router.
  - **Ultrasonic / ToF Close Obstacle (`front_distance_cm < 30`)**: Labeled simulated trigger injected via `raw_sensors` (while camera `range_m` provides the real optical equivalent).
