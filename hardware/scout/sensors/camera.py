class ScoutCameraStreamer:
    """
    Direct MJPEG camera streamer interface on SCOUT's Raspberry Pi (TRD §8, Architecture §8.3).
    Serves video frames directly to the React dashboard over HTTP MJPEG, bypassing FastAPI.
    """

    def __init__(self, port: int = 8081) -> None:
        self.port = port
        self.is_active = False

    def get_stream_url(self, host: str = "127.0.0.1") -> str:
        """Returns direct HTTP MJPEG endpoint for React dashboard."""
        return f"http://{host}:{self.port}/stream.mjpg"
