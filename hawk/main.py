import asyncio
import os
from hawk.flight_interface.flight_controller import FlightControllerInterface
from hawk.sensors.telemetry import HawkTelemetryMonitor
from hawk.sensors.camera import HawkCameraStreamer
from hawk.communication.companion_link import HawkCompanionLink


async def main() -> None:
    print("=== STARTING HAWK01 AERIAL DRONE SYSTEM ===")
    fc = FlightControllerInterface()
    telemetry = HawkTelemetryMonitor()
    camera = HawkCameraStreamer(port=8082)
    link = HawkCompanionLink(
        server_url=os.getenv("SHADOW_BACKEND_WS_URL", "ws://127.0.0.1:8000/ws/vehicle"),
        auth_token=os.getenv("SHADOW_VEHICLE_SHARED_SECRET", "provisional_secret_change_me")
    )

    print(f"HAWK Camera Stream ready at: {camera.get_stream_url()}")
    print("Ready for ULTRON aerial scan task dispatch.")


if __name__ == "__main__":
    asyncio.run(main())
