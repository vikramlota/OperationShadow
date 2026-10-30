"""Static indoor floor-plan configuration and 3D SLAM map upload REST endpoints (§3, §7, §8.2).

Serves a clearly labeled provisional simulation floor-plan configuration until
Hardware & IoT doc §11 indoor dimensions and geofence coordinates are supplied,
and provides multipart upload handling for heavy 3D SLAM mapping files (visual_map.ply,
lidar_map.pcd, rtabmap.db) from physical robots streaming safely into saved_maps/.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from schemas.entities import (
    FloorPlanConfigResponse,
    FloorPlanGeofenceSchema,
    FloorPlanRoomSchema,
    FloorPlanWaypointSchema,
    MapUploadResponse,
)

router = APIRouter(prefix="/map", tags=["map"])
upload_map_router = APIRouter(tags=["map"])

VEHICLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

ALLOWED_MAP_FILES = {
    "visual_map.ply": "visual_map.ply",
    "lidar_map.pcd": "lidar_map.pcd",
    "rtabmap.db": "rtabmap.db",
}

DEFAULT_PROVISIONAL_FLOOR_PLAN = FloorPlanConfigResponse(
    is_provisional_simulation_config=True,
    coordinate_system="local_cartesian_meters",
    width_m=20.0,
    height_m=15.0,
    rooms=[
        FloorPlanRoomSchema(
            id="SECTOR_A",
            name="Sector Alpha — Entry Corridor",
            x=0.0,
            y=0.0,
            width=10.0,
            height=7.5,
        ),
        FloorPlanRoomSchema(
            id="SECTOR_B",
            name="Sector Bravo — Main Bay",
            x=10.0,
            y=0.0,
            width=10.0,
            height=15.0,
        ),
        FloorPlanRoomSchema(
            id="SECTOR_C",
            name="Sector Charlie — Staging Room",
            x=0.0,
            y=7.5,
            width=10.0,
            height=7.5,
        ),
    ],
    waypoints=[
        FloorPlanWaypointSchema(id="WP_BASE", label="Base Station", x=1.5, y=1.5),
        FloorPlanWaypointSchema(id="WP_ALPHA_MID", label="Alpha Checkpoint", x=5.0, y=3.75),
        FloorPlanWaypointSchema(id="WP_BRAVO_CENTER", label="Bravo Overwatch", x=15.0, y=7.5),
        FloorPlanWaypointSchema(id="WP_CHARLIE_NORTH", label="Charlie Staging", x=5.0, y=11.25),
    ],
    hawk_geofence=FloorPlanGeofenceSchema(
        id="GEOFENCE_HAWK01",
        label="HAWK01 Indoor Flight Envelope (Provisional)",
        x=1.0,
        y=1.0,
        width=18.0,
        height=13.0,
        max_altitude_m=3.0,
    ),
)


@router.get("", response_model=FloorPlanConfigResponse)
async def get_map_endpoint(request: Request) -> FloorPlanConfigResponse:
    """Return static indoor floor-plan configuration for the dashboard SVG map (§7, §8.2)."""
    custom_map: FloorPlanConfigResponse | None = getattr(
        request.app.state, "floor_plan_config", None
    )
    return custom_map if custom_map is not None else DEFAULT_PROVISIONAL_FLOOR_PLAN


async def process_map_upload(
    vehicle_id: str,
    visual_map: UploadFile | None,
    visual_map_ply: UploadFile | None,
    visual_map_underscore: UploadFile | None,
    lidar_map: UploadFile | None,
    lidar_map_pcd: UploadFile | None,
    lidar_map_underscore: UploadFile | None,
    rtabmap: UploadFile | None,
    rtabmap_db: UploadFile | None,
    rtabmap_underscore: UploadFile | None,
    files: list[UploadFile],
) -> MapUploadResponse:
    """Securely stream incoming 3D SLAM mapping files to saved_maps/ using chunked streaming."""
    clean_vid = vehicle_id.strip()
    if not VEHICLE_ID_PATTERN.match(clean_vid) or ".." in clean_vid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid vehicle_id '{vehicle_id}'. Must be alphanumeric with underscores/hyphens only.",
        )

    saved_dir = Path("saved_maps")
    saved_dir.mkdir(parents=True, exist_ok=True)

    candidates: dict[str, UploadFile] = {}

    def _consider(target_name: str, uf: UploadFile | None) -> None:
        if uf is not None and uf.filename and target_name not in candidates:
            candidates[target_name] = uf

    # Slot 1: visual_map.ply
    _consider("visual_map.ply", visual_map)
    _consider("visual_map.ply", visual_map_ply)
    _consider("visual_map.ply", visual_map_underscore)

    # Slot 2: lidar_map.pcd
    _consider("lidar_map.pcd", lidar_map)
    _consider("lidar_map.pcd", lidar_map_pcd)
    _consider("lidar_map.pcd", lidar_map_underscore)

    # Slot 3: rtabmap.db
    _consider("rtabmap.db", rtabmap)
    _consider("rtabmap.db", rtabmap_db)
    _consider("rtabmap.db", rtabmap_underscore)

    # Also inspect generic multi-file list if provided
    for f in files:
        if f is not None and f.filename:
            base_name = Path(f.filename).name.lower()
            if base_name in ALLOWED_MAP_FILES and base_name not in candidates:
                candidates[base_name] = f
            elif base_name.endswith(".ply") and "visual_map.ply" not in candidates:
                candidates["visual_map.ply"] = f
            elif base_name.endswith(".pcd") and "lidar_map.pcd" not in candidates:
                candidates["lidar_map.pcd"] = f
            elif base_name.endswith(".db") and "rtabmap.db" not in candidates:
                candidates["rtabmap.db"] = f

    saved_filenames: list[str] = []
    for filename, upload_file in candidates.items():
        safe_filename = Path(filename).name
        target_path = saved_dir / safe_filename

        try:
            with target_path.open("wb") as dest:
                shutil.copyfileobj(upload_file.file, dest)
            saved_filenames.append(safe_filename)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to stream and save file {safe_filename}: {exc}",
            ) from exc

    return MapUploadResponse(
        status="success",
        vehicle_id=clean_vid,
        saved_files=saved_filenames,
        filenames=saved_filenames,
    )


@upload_map_router.post("/upload-map/{vehicle_id}", response_model=MapUploadResponse)
async def upload_map_root_endpoint(
    vehicle_id: str,
    visual_map: UploadFile | None = File(default=None, description="Colored 3D map (.ply)"),
    visual_map_ply: UploadFile | None = File(default=None, alias="visual_map.ply"),
    visual_map_underscore: UploadFile | None = File(default=None, alias="visual_map_ply"),
    lidar_map: UploadFile | None = File(default=None, description="LiDAR point cloud (.pcd)"),
    lidar_map_pcd: UploadFile | None = File(default=None, alias="lidar_map.pcd"),
    lidar_map_underscore: UploadFile | None = File(default=None, alias="lidar_map_pcd"),
    rtabmap: UploadFile | None = File(default=None, description="RTAB-Map database (.db)"),
    rtabmap_db: UploadFile | None = File(default=None, alias="rtabmap.db"),
    rtabmap_underscore: UploadFile | None = File(default=None, alias="rtabmap_db"),
    files: list[UploadFile] = File(default=[]),
) -> MapUploadResponse:
    """Upload heavy 3D SLAM mapping files from physical robots at /upload-map/{vehicle_id}."""
    return await process_map_upload(
        vehicle_id=vehicle_id,
        visual_map=visual_map,
        visual_map_ply=visual_map_ply,
        visual_map_underscore=visual_map_underscore,
        lidar_map=lidar_map,
        lidar_map_pcd=lidar_map_pcd,
        lidar_map_underscore=lidar_map_underscore,
        rtabmap=rtabmap,
        rtabmap_db=rtabmap_db,
        rtabmap_underscore=rtabmap_underscore,
        files=files,
    )


@router.post("/upload-map/{vehicle_id}", response_model=MapUploadResponse)
async def upload_map_prefixed_endpoint(
    vehicle_id: str,
    visual_map: UploadFile | None = File(default=None, description="Colored 3D map (.ply)"),
    visual_map_ply: UploadFile | None = File(default=None, alias="visual_map.ply"),
    visual_map_underscore: UploadFile | None = File(default=None, alias="visual_map_ply"),
    lidar_map: UploadFile | None = File(default=None, description="LiDAR point cloud (.pcd)"),
    lidar_map_pcd: UploadFile | None = File(default=None, alias="lidar_map.pcd"),
    lidar_map_underscore: UploadFile | None = File(default=None, alias="lidar_map_pcd"),
    rtabmap: UploadFile | None = File(default=None, description="RTAB-Map database (.db)"),
    rtabmap_db: UploadFile | None = File(default=None, alias="rtabmap.db"),
    rtabmap_underscore: UploadFile | None = File(default=None, alias="rtabmap_db"),
    files: list[UploadFile] = File(default=[]),
) -> MapUploadResponse:
    """Alias for map upload under /map/upload-map/{vehicle_id}."""
    return await process_map_upload(
        vehicle_id=vehicle_id,
        visual_map=visual_map,
        visual_map_ply=visual_map_ply,
        visual_map_underscore=visual_map_underscore,
        lidar_map=lidar_map,
        lidar_map_pcd=lidar_map_pcd,
        lidar_map_underscore=lidar_map_underscore,
        rtabmap=rtabmap,
        rtabmap_db=rtabmap_db,
        rtabmap_underscore=rtabmap_underscore,
        files=files,
    )
