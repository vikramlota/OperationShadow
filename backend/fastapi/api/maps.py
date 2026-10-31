"""Alias module for map.py supporting legacy /maps.py imports."""
from api.map import router, upload_map_router

__all__ = ["router", "upload_map_router"]
