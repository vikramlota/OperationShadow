"""Dedicated read-only ROS 2 Humble ROSBridge adapter for rover perception (`/detections_json`).

Architectural & Safety Guarantees:
1. Strictly read-only perception subscriber: subscribes ONLY to the configured
   detection topic (`/detections_json`, default ROS 2 Humble message type
   `std_msgs/msg/String`, with compatibility for `std_msgs/String` and standard
   ROSBridge v2 JSON protocol envelopes).
2. NEVER publishes ROS messages, NEVER calls ROS services or action servers,
   NEVER subscribes to undocumented command interfaces, and NEVER enables
   physical actuator control (`SHADOW_HARDWARE_LOCKOUT` and `LockedRealVehicleAdapter`
   remain strictly enforced).
3. Isolates Twisted/Autobahn callbacks from the FastAPI asyncio event loop via
   `loop.call_soon_threadsafe` and a bounded `asyncio.Queue`, guaranteeing that
   ROS callbacks never block the event loop or perform synchronous SQLite I/O.
4. Supports clean startup, automatic reconnect state tracking, and clean
   unsubscribe/termination on application shutdown, while degrading gracefully
   if the rover is unreachable so the mock fleet continues operating normally.
5. Provides replaceable extension-point hooks (`OptionalTopicExtensionPort`) for
   future `/oak/rgb/image_raw/compressed` (`sensor_msgs/msg/CompressedImage`) and
   `/oak/vio/odometry` (`nav_msgs/msg/Odometry`) topics without speculatively
   parsing or subscribing to unconfirmed schemas.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from core.logging import get_logger
import roslibpy
from schemas.enums import RosConnectionStateEnum

logger = get_logger("services.rosbridge")

ROS2_DETECTIONS_MESSAGE_TYPE = "std_msgs/msg/String"
ROS1_DETECTIONS_MESSAGE_TYPE = "std_msgs/String"
DETECTIONS_MESSAGE_TYPE = ROS2_DETECTIONS_MESSAGE_TYPE


def build_rosbridge_subscribe_envelope(
    topic: str,
    message_type: str | None = ROS2_DETECTIONS_MESSAGE_TYPE,
) -> dict[str, Any]:
    """Construct a standard ROSBridge v2 JSON protocol `subscribe` operation envelope."""
    envelope: dict[str, Any] = {
        "op": "subscribe",
        "topic": topic,
    }
    if message_type and message_type.strip():
        envelope["type"] = message_type.strip()
    return envelope


def build_rosbridge_unsubscribe_envelope(topic: str) -> dict[str, Any]:
    """Construct a standard ROSBridge v2 JSON protocol `unsubscribe` operation envelope."""
    return {
        "op": "unsubscribe",
        "topic": topic,
    }


@runtime_checkable
class RosClientLike(Protocol):
    """Protocol matching `roslibpy.Ros` for dependency injection and offline testing."""

    @property
    def is_connected(self) -> bool:
        ...

    def run(self, timeout: int = 10) -> None:
        ...

    def on(self, event_name: str, callback: Callable[..., Any]) -> None:
        ...

    def on_ready(self, callback: Callable[..., Any], run_in_thread: bool = True) -> None:
        ...

    def close(self, timeout: int = 10) -> None:
        ...

    def terminate(self) -> None:
        ...


@runtime_checkable
class RosTopicLike(Protocol):
    """Protocol matching `roslibpy.Topic` for read-only subscription."""

    @property
    def is_subscribed(self) -> bool:
        ...

    def subscribe(self, callback: Callable[[dict[str, Any]], Any]) -> None:
        ...

    def unsubscribe(self) -> None:
        ...


@runtime_checkable
class OptionalTopicExtensionPort(Protocol):
    """Replaceable extension point for future optional rover topics (Section 9).

    Note: `/oak/rgb/image_raw/compressed` (expected `sensor_msgs/msg/CompressedImage`)
    and `/oak/vio/odometry` (expected `nav_msgs/msg/Odometry`) require live sample
    confirmation via `ros2 topic info` before subscribing. No subscription or
    speculative parsing is performed in this milestone.
    """

    @property
    def topic_name(self) -> str:
        ...

    @property
    def message_type(self) -> str:
        ...


RosClientFactory = Callable[[str, int], RosClientLike]
RosTopicFactory = Callable[[RosClientLike, str, str], RosTopicLike]
RawDetectionCallback = Callable[[dict[str, Any] | str], Awaitable[None]]
ConnectionStateCallback = Callable[[RosConnectionStateEnum], Awaitable[None]]


def _default_ros_client_factory(host: str, port: int) -> RosClientLike:
    """Instantiate a real `roslibpy.Ros` client targeting `ws://<host>:<port>`."""
    client: Any = roslibpy.Ros(host=host, port=port)
    return client  # type: ignore[no-any-return]


def _default_ros_topic_factory(
    ros_client: RosClientLike,
    topic_name: str,
    message_type: str,
) -> RosTopicLike:
    """Instantiate a real `roslibpy.Topic` subscriber with reconnect_on_close enabled."""
    topic: Any = roslibpy.Topic(
        ros=ros_client,
        name=topic_name,
        message_type=message_type,
        reconnect_on_close=True,
    )
    return topic  # type: ignore[no-any-return]


@dataclass(frozen=True)
class RosBridgeAdapterConfig:
    """Validated configuration for the read-only ROS 2 Humble detection subscriber."""

    enabled: bool = False
    host: str = ""
    port: int = 9090
    vehicle_id: str = ""
    detections_topic: str = "/detections_json"
    message_type: str = DETECTIONS_MESSAGE_TYPE
    queue_maxsize: int = 128
    connect_timeout_seconds: float = 3.0
    reconnect_interval_seconds: float = 2.0


class RosBridgeAdapter:
    """Manages the read-only `roslibpy` connection and `/detections_json` subscription."""

    def __init__(
        self,
        config: RosBridgeAdapterConfig,
        *,
        on_detection_message: RawDetectionCallback | None = None,
        on_connection_state_change: ConnectionStateCallback | None = None,
        ros_client_factory: RosClientFactory | None = None,
        ros_topic_factory: RosTopicFactory | None = None,
    ) -> None:
        if config.queue_maxsize < 1:
            msg = "queue_maxsize must be >= 1"
            raise ValueError(msg)

        self._config = config
        self._on_detection_message = on_detection_message
        self._on_connection_state_change = on_connection_state_change
        self._client_factory: RosClientFactory = (
            ros_client_factory or _default_ros_client_factory
        )
        self._topic_factory: RosTopicFactory = (
            ros_topic_factory or _default_ros_topic_factory
        )

        self._connection_state: RosConnectionStateEnum = (
            RosConnectionStateEnum.DISCONNECTED
            if config.enabled
            else RosConnectionStateEnum.DISABLED
        )
        self._ros_client: RosClientLike | None = None
        self._detections_topic: RosTopicLike | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[dict[str, Any] | str] = asyncio.Queue(
            maxsize=config.queue_maxsize
        )
        self._worker_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._running: bool = False
        self._has_connected_once: bool = False
        self._dropped_queue_messages: int = 0
        self._received_messages_count: int = 0

    @property
    def config(self) -> RosBridgeAdapterConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def vehicle_id(self) -> str:
        return self._config.vehicle_id

    @property
    def topic_name(self) -> str:
        return self._config.detections_topic

    @property
    def message_type(self) -> str:
        return self._config.message_type

    @property
    def connection_state(self) -> RosConnectionStateEnum:
        return self._connection_state

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def dropped_queue_messages(self) -> int:
        return self._dropped_queue_messages

    @property
    def received_messages_count(self) -> int:
        return self._received_messages_count

    def set_callbacks(
        self,
        *,
        on_detection_message: RawDetectionCallback | None = None,
        on_connection_state_change: ConnectionStateCallback | None = None,
    ) -> None:
        if on_detection_message is not None:
            self._on_detection_message = on_detection_message
        if on_connection_state_change is not None:
            self._on_connection_state_change = on_connection_state_change

    async def _set_connection_state(self, new_state: RosConnectionStateEnum) -> None:
        if self._connection_state == new_state:
            return
        self._connection_state = new_state
        if self._on_connection_state_change is not None:
            try:
                await self._on_connection_state_change(new_state)
            except Exception as exc:
                logger.error(f"Error in ROS connection state callback: {exc}")

    def _schedule_state_change_from_thread(
        self,
        new_state: RosConnectionStateEnum,
    ) -> None:
        loop = self._loop
        if loop is None or loop.is_closed() or not self._running:
            self._connection_state = new_state
            return

        def _apply() -> None:
            if not self._running:
                return
            asyncio.create_task(self._set_connection_state(new_state))

        try:
            loop.call_soon_threadsafe(_apply)
        except RuntimeError:
            self._connection_state = new_state

    def _enqueue_message_nonblocking(self, message: dict[str, Any] | str) -> None:
        """Bounded queue insertion executed on the asyncio event loop thread."""
        if not self._running:
            return
        self._received_messages_count += 1
        if self._queue.full():
            with suppress(asyncio.QueueEmpty):
                _ = self._queue.get_nowait()
                self._queue.task_done()
            self._dropped_queue_messages += 1
            logger.warning(
                "ROSBridge detection queue reached maxsize; dropped oldest queued frame."
            )
        with suppress(asyncio.QueueFull):
            self._queue.put_nowait(message)

    def handle_ros_topic_callback(self, message: dict[str, Any] | str) -> None:
        """Thread-safe callback invoked by `roslibpy.Topic.subscribe`.

        Never performs database writes or blocking work on the Twisted reactor thread.
        """
        loop = self._loop
        if loop is None or loop.is_closed() or not self._running:
            return
        try:
            loop.call_soon_threadsafe(self._enqueue_message_nonblocking, message)
        except RuntimeError:
            return

    async def _queue_worker_loop(self) -> None:
        """Asyncio background consumer that forwards queued ROS frames to PerceptionService."""
        try:
            while self._running:
                message = await self._queue.get()
                try:
                    if self._on_detection_message is not None:
                        await self._on_detection_message(message)
                except Exception as exc:
                    logger.error(f"Error processing ROS detection frame: {exc}")
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise

    def _on_ros_connected_event(self, *_args: Any, **_kwargs: Any) -> None:
        self._has_connected_once = True
        logger.info(
            f"ROSBridge connected to ws://{self._config.host}:{self._config.port} "
            f"(vehicle={self._config.vehicle_id}, topic={self._config.detections_topic}, "
            f"type={self._config.message_type})."
        )
        self._schedule_state_change_from_thread(RosConnectionStateEnum.CONNECTED)

    def _on_ros_closing_event(self, *_args: Any, **_kwargs: Any) -> None:
        if not self._running:
            self._schedule_state_change_from_thread(RosConnectionStateEnum.DISCONNECTED)
            return
        next_state = (
            RosConnectionStateEnum.RECONNECTING
            if self._has_connected_once
            else RosConnectionStateEnum.DISCONNECTED
        )
        logger.warning(
            f"ROSBridge connection closed for ws://{self._config.host}:{self._config.port}; "
            f"state={next_state.value}."
        )
        self._schedule_state_change_from_thread(next_state)

    def _on_ros_error_event(self, *_args: Any, **_kwargs: Any) -> None:
        if not self._running:
            return
        next_state = (
            RosConnectionStateEnum.RECONNECTING
            if self._has_connected_once
            else RosConnectionStateEnum.DISCONNECTED
        )
        logger.warning(
            f"ROSBridge transport error on ws://{self._config.host}:{self._config.port}; "
            f"state={next_state.value}."
        )
        self._schedule_state_change_from_thread(next_state)

    async def _attempt_connect_and_subscribe(self) -> bool:
        """Initialize client and topic subscription without crashing on failure."""
        if not self._running:
            return False

        try:
            if self._ros_client is None:
                client = self._client_factory(self._config.host, self._config.port)
                client.on("connection", self._on_ros_connected_event)
                client.on("closing", self._on_ros_closing_event)
                client.on("error", self._on_ros_error_event)
                self._ros_client = client

            if self._detections_topic is None and self._ros_client is not None:
                topic = self._topic_factory(
                    self._ros_client,
                    self._config.detections_topic,
                    self._config.message_type,
                )
                topic.subscribe(self.handle_ros_topic_callback)
                self._detections_topic = topic

            assert self._ros_client is not None
            await asyncio.to_thread(
                self._ros_client.run,
                int(max(1.0, self._config.connect_timeout_seconds)),
            )

            if self._ros_client.is_connected:
                self._has_connected_once = True
                await self._set_connection_state(RosConnectionStateEnum.CONNECTED)
                return True

            await self._set_connection_state(
                RosConnectionStateEnum.RECONNECTING
                if self._has_connected_once
                else RosConnectionStateEnum.DISCONNECTED
            )
            return False
        except Exception as exc:
            logger.warning(
                f"ROSBridge connection attempt to ws://{self._config.host}:{self._config.port} "
                f"did not succeed ({exc}). Backend continues running normally."
            )
            await self._set_connection_state(
                RosConnectionStateEnum.RECONNECTING
                if self._has_connected_once
                else RosConnectionStateEnum.DISCONNECTED
            )
            return False

    async def _connection_supervisor_loop(self) -> None:
        """Monitor ROSBridge connection state and retry cleanly when disconnected."""
        try:
            while self._running:
                if self._ros_client is None or not self._ros_client.is_connected:
                    await self._attempt_connect_and_subscribe()
                elif self._connection_state != RosConnectionStateEnum.CONNECTED:
                    await self._set_connection_state(RosConnectionStateEnum.CONNECTED)

                await asyncio.sleep(self._config.reconnect_interval_seconds)
        except asyncio.CancelledError:
            raise

    async def start(self) -> None:
        """Start the bounded queue worker and non-blocking ROSBridge connection supervisor."""
        if not self._config.enabled:
            await self._set_connection_state(RosConnectionStateEnum.DISABLED)
            return
        if self._running:
            return

        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self._config.queue_maxsize)
        self._running = True
        await self._set_connection_state(RosConnectionStateEnum.CONNECTING)

        self._worker_task = asyncio.create_task(
            self._queue_worker_loop(),
            name=f"rosbridge-queue-worker-{self._config.vehicle_id}",
        )
        # Perform initial connection attempt; failure must never crash application startup
        await self._attempt_connect_and_subscribe()
        self._reconnect_task = asyncio.create_task(
            self._connection_supervisor_loop(),
            name=f"rosbridge-supervisor-{self._config.vehicle_id}",
        )

    async def stop(self) -> None:
        """Cleanly unsubscribe from `/detections_json`, close client, and stop tasks."""
        self._running = False

        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError, RuntimeError):
                await self._reconnect_task
            self._reconnect_task = None

        if self._worker_task is not None:
            self._worker_task.cancel()
            with suppress(asyncio.CancelledError, RuntimeError):
                await self._worker_task
            self._worker_task = None

        while not self._queue.empty():
            with suppress(asyncio.QueueEmpty, RuntimeError):
                _ = self._queue.get_nowait()
                self._queue.task_done()

        topic = self._detections_topic
        self._detections_topic = None
        if topic is not None:
            try:
                await asyncio.to_thread(topic.unsubscribe)
            except Exception as exc:
                logger.warning(f"Error unsubscribing from ROS topic: {exc}")

        client = self._ros_client
        self._ros_client = None
        if client is not None:
            try:
                await asyncio.to_thread(client.close)
            except Exception as exc:
                logger.warning(f"Error closing ROSBridge client: {exc}")
            try:
                await asyncio.to_thread(client.terminate)
            except Exception as exc:
                logger.debug(f"ROSBridge terminate notice: {exc}")

        final_state = (
            RosConnectionStateEnum.DISCONNECTED
            if self._config.enabled
            else RosConnectionStateEnum.DISABLED
        )
        await self._set_connection_state(final_state)
