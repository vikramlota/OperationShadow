class HawkCameraStreamer:
    """
    Direct MJPEG camera streamer interface on HAWK companion link (TRD §9, Architecture §8.3).
    Serves overhead vantage-point video directly to React dashboard over HTTP MJPEG.
    """

    def __init__(self, port: int = 8082) -> None:
        self.port = port
        self.is_active = False

    def get_stream_url(self, host: str = "127.0.0.1") -> str:
        """Returns direct HTTP MJPEG endpoint for React dashboard."""
        return f"http://{host}:{self.port}/stream.mjpg"
