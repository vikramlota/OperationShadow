"""Static indoor floor-plan configuration REST endpoint (§3, §7, §8.2: GET /map).

Serves a clearly labeled provisional simulation floor-plan configuration until
Hardware & IoT doc §11 indoor dimensions and geofence coordinates are supplied.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from schemas.entities import (
    FloorPlanConfigResponse,
    FloorPlanGeofenceSchema,
    FloorPlanRoomSchema,
    FloorPlanWaypointSchema,
)

router = APIRouter(prefix="/map", tags=["map"])

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
