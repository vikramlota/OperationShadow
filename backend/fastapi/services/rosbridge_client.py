"""ROSBridge WebSocket client for SCOUT01 real-hardware integration.

Connects to the rover's ROSBridge server (ws://10.241.171.41:9090) as a
read-only subscriber. Streams live detections from /detections_json and
forwards them into the ULTRON World Model via the MissionService agent.

Architecture constraints (AGENTS.md):
- SHADOW_HARDWARE_LOCKOUT=true  → observation ingest only, no command publishing
- Camera streams are fetched directly by the React dashboard (never proxied here)
- All updates pushed over dashboard WebSocket, never polled
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("operation_shadow.rosbridge")

ROSBRIDGE_URL = "ws://10.241.171.41:9090"
DETECTION_TOPIC = "/detections_json"
RECONNECT_DELAY_SEC = 5.0


class RosBridgeClient:
    """Async ROSBridge v2 client — read-only subscriber for SCOUT01 topics."""

    def __init__(
        self,
        url: str = ROSBRIDGE_URL,
        on_detection: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._url = url
        self._on_detection = on_detection
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self.connected = False

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background connection loop (non-blocking)."""
        self._running = True
        self._task = asyncio.create_task(self._connection_loop(), name="rosbridge-client")
        logger.info("ROSBridge client started → %s", self._url)

    async def stop(self) -> None:
        """Gracefully stop the client."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.connected = False
        logger.info("ROSBridge client stopped.")

    # ── Internal loop ─────────────────────────────────────────────────────────

    async def _connection_loop(self) -> None:
        """Reconnect forever until stopped."""
        while self._running:
            try:
                await self._run_session()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.connected = False
                logger.warning(
                    "ROSBridge disconnected (%s: %s). Reconnecting in %.0fs…",
                    type(exc).__name__, exc, RECONNECT_DELAY_SEC,
                )
                await asyncio.sleep(RECONNECT_DELAY_SEC)

    async def _run_session(self) -> None:
        try:
            import websockets
        except ImportError:
            logger.error("websockets package not installed. Run: pip install websockets")
            await asyncio.sleep(RECONNECT_DELAY_SEC)
            return

        logger.info("Connecting to ROSBridge at %s …", self._url)
        async with websockets.connect(self._url, open_timeout=10) as ws:
            self.connected = True
            logger.info("ROSBridge CONNECTED to SCOUT01 ✓")

            # Subscribe to detection topic
            await ws.send(json.dumps({
                "op": "subscribe",
                "topic": DETECTION_TOPIC,
                "type": "std_msgs/msg/String",
                "throttle_rate": 200,   # max 5 Hz to avoid flooding
                "queue_length": 1,
            }))
            logger.info("Subscribed to %s", DETECTION_TOPIC)

            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                    if msg.get("op") == "publish" and msg.get("topic") == DETECTION_TOPIC:
                        payload_str = msg.get("msg", {}).get("data", "")
                        if payload_str and self._on_detection:
                            detection = json.loads(payload_str)
                            await self._on_detection(detection)
                except Exception as exc:
                    logger.debug("Failed to parse ROSBridge message: %s", exc)

        self.connected = False


# ── Singleton & lifespan integration ─────────────────────────────────────────

_client: RosBridgeClient | None = None


def get_rosbridge_client() -> RosBridgeClient | None:
    return _client


async def start_rosbridge(mission_service_agent: Any | None = None) -> RosBridgeClient:
    """Create and start the singleton ROSBridge client."""
    global _client

    async def _handle_detection(detection: dict[str, Any]) -> None:
        """Forward SCOUT01 detections into ULTRON World Model."""
        objects = detection.get("objects", [])
        if not objects or mission_service_agent is None:
            return

        for obj in objects:
            cls = obj.get("class", "unknown")
            score = float(obj.get("score", 0.0))
            pos = obj.get("position", {})
            x = float(pos.get("x", 0.0))
            y = float(pos.get("y", 0.0))

            if score < 0.5:
                continue  # skip low-confidence detections

            # Map OAK-D detection class → ULTRON observation type
            obs_type = _class_to_obs_type(cls)

            logger.info(
                "SCOUT01 detection → %s (%.0f%%) at (%.2f, %.2f)",
                obs_type, score * 100, x, y,
            )

            try:
                from ultron_stub.interface import ObservationPayloadStub
                obs = ObservationPayloadStub(
                    observation_id=f"ros-{int(datetime.now(timezone.utc).timestamp()*1000)}",
                    mission_id=getattr(mission_service_agent, "_active_mission_id", None) or "live",
                    vehicle_id="SCOUT01",
                    obs_type=obs_type,
                    confidence=score,
                    location_x=x,
                    location_y=y,
                    timestamp=datetime.now(timezone.utc),
                )
                await mission_service_agent.record_observation(obs)
            except Exception as exc:
                logger.debug("Failed to forward observation: %s", exc)

    _client = RosBridgeClient(url=ROSBRIDGE_URL, on_detection=_handle_detection)
    _client.start()
    return _client


def _class_to_obs_type(cls: str) -> str:
    """Map OAK-D YOLO class names to ULTRON observation event types."""
    mapping = {
        "person": "target_detected",
        "fire": "hazard_detected",
        "smoke": "hazard_detected",
        "box": "target_detected",
        "backpack": "target_detected",
        "suitcase": "target_detected",
        "chair": "obstacle_detected",
        "bench": "obstacle_detected",
        "bottle": "target_detected",
    }
    return mapping.get(cls.lower(), "target_detected")
