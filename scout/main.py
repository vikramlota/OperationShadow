import asyncio
import os
from scout.navigation.controller import GroundNavigationController
from scout.sensors.fusion import SensorFusion
from scout.sensors.camera import ScoutCameraStreamer
from scout.communication.websocket_client import ScoutWebSocketClient
from scout.communication.serial_bridge import ESP32SerialBridge


async def main() -> None:
    print("=== STARTING SCOUT01 GROUND ROVER SYSTEM ===")
    nav = GroundNavigationController()
    fusion = SensorFusion()
    camera = ScoutCameraStreamer(port=8081)
    ws_client = ScoutWebSocketClient(
        server_url=os.getenv("SHADOW_BACKEND_WS_URL", "ws://127.0.0.1:8000/ws/vehicle"),
        auth_token=os.getenv("SHADOW_VEHICLE_SHARED_SECRET", "provisional_secret_change_me")
    )
    serial_bridge = ESP32SerialBridge()

    print(f"SCOUT Camera Stream ready at: {camera.get_stream_url()}")
    print("Ready for ULTRON task dispatch.")


if __name__ == "__main__":
    asyncio.run(main())
