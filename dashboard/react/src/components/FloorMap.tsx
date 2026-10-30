import React, { useEffect, useState } from "react";
import {
  fetchFloorMap,
  type FloorPlanConfigPayload,
  type VehicleId,
} from "../api/client";
import { useDashboardStore, type VehicleSummaryState } from "../state/store";

export const MAP_VIEWBOX_WIDTH = 680;
export const MAP_VIEWBOX_HEIGHT = 480;
export const MAP_PADDING = 36;

/**
 * Explicit, deterministic indoor world-to-SVG coordinate transformation helpers.
 * Maps indoor Cartesian coordinates [0..widthM, 0..heightM] into the SVG viewport.
 */
export function worldToSvgX(
  xMeters: number,
  widthMeters: number,
  viewWidth: number = MAP_VIEWBOX_WIDTH,
  padding: number = MAP_PADDING,
): number {
  const safeWidth = widthMeters > 0 ? widthMeters : 20;
  const usableWidth = viewWidth - padding * 2;
  return Number((padding + (xMeters / safeWidth) * usableWidth).toFixed(2));
}

export function worldToSvgY(
  yMeters: number,
  heightMeters: number,
  viewHeight: number = MAP_VIEWBOX_HEIGHT,
  padding: number = MAP_PADDING,
): number {
  const safeHeight = heightMeters > 0 ? heightMeters : 15;
  const usableHeight = viewHeight - padding * 2;
  return Number((padding + (yMeters / safeHeight) * usableHeight).toFixed(2));
}

export function worldRectToSvg(
  x: number,
  y: number,
  w: number,
  h: number,
  widthMeters: number,
  heightMeters: number,
): { x: number; y: number; width: number; height: number } {
  const x1 = worldToSvgX(x, widthMeters);
  const y1 = worldToSvgY(y, heightMeters);
  const x2 = worldToSvgX(x + w, widthMeters);
  const y2 = worldToSvgY(y + h, heightMeters);
  return {
    x: x1,
    y: y1,
    width: Number(Math.max(0, x2 - x1).toFixed(2)),
    height: Number(Math.max(0, y2 - y1).toFixed(2)),
  };
}

function getVehicleHealthLabel(vehicle: VehicleSummaryState): string {
  if (vehicle.connection === "DISCONNECTED") {
    return "DISCONNECTED";
  }
  if (vehicle.connection === "UNKNOWN" || vehicle.status === "UNKNOWN") {
    return "UNKNOWN";
  }
  if (vehicle.isStale) {
    return "STALE";
  }
  return vehicle.status;
}

const FALLBACK_SIMULATION_MAP: FloorPlanConfigPayload = {
  is_provisional_simulation_config: true,
  coordinate_system: "local_cartesian_meters",
  width_m: 20,
  height_m: 15,
  rooms: [
    {
      id: "SECTOR_A",
      name: "Sector Alpha — Entry Corridor",
      x: 0,
      y: 0,
      width: 10,
      height: 7.5,
    },
    {
      id: "SECTOR_B",
      name: "Sector Bravo — Main Bay",
      x: 10,
      y: 0,
      width: 10,
      height: 15,
    },
    {
      id: "SECTOR_C",
      name: "Sector Charlie — Staging Room",
      x: 0,
      y: 7.5,
      width: 10,
      height: 7.5,
    },
  ],
  waypoints: [
    { id: "WP_BASE", label: "Base Station", x: 1.5, y: 1.5 },
    { id: "WP_ALPHA_MID", label: "Alpha Checkpoint", x: 5.0, y: 3.75 },
    { id: "WP_BRAVO_CENTER", label: "Bravo Overwatch", x: 15.0, y: 7.5 },
  ],
  hawk_geofence: {
    id: "GEOFENCE_HAWK01",
    label: "HAWK01 Indoor Flight Envelope (Provisional)",
    x: 1.0,
    y: 1.0,
    width: 18.0,
    height: 13.0,
    max_altitude_m: 3.0,
  },
};

export function FloorMap(): React.JSX.Element {
  const { state, dispatch } = useDashboardStore();
  const [mapError, setMapError] = useState<string | null>(null);

  useEffect(() => {
    if (state.floorPlan !== null) {
      return;
    }
    let cancelled = false;
    fetchFloorMap()
      .then((config) => {
        if (!cancelled) {
          dispatch({ type: "SET_FLOOR_PLAN", payload: config });
          setMapError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setMapError(
            err instanceof Error
              ? err.message
              : "Unable to load /map floor configuration",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [state.floorPlan, dispatch]);

  const activeMap = state.floorPlan ?? FALLBACK_SIMULATION_MAP;
  const widthM = activeMap.width_m;
  const heightM = activeMap.height_m;
  const geofenceRect = worldRectToSvg(
    activeMap.hawk_geofence.x,
    activeMap.hawk_geofence.y,
    activeMap.hawk_geofence.width,
    activeMap.hawk_geofence.height,
    widthM,
    heightM,
  );

  const xTicks = [0, 5, 10, 15, 20].filter((t) => t <= widthM);
  const yTicks = [0, 5, 10, 15].filter((t) => t <= heightM);

  const selectVehicle = (vehicleId: VehicleId): void => {
    dispatch({ type: "SELECT_VEHICLE", payload: vehicleId });
  };

  return (
    <section className="c2-panel" aria-label="SVG Floor Map">
      <div className="c2-panel-header">
        <h2 className="c2-panel-title">
          TACTICAL INDOOR FLOOR MAP // {activeMap.coordinate_system.toUpperCase()}
        </h2>
        <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
          {activeMap.is_provisional_simulation_config && (
            <span
              className="c2-badge c2-badge-warning"
              title="Hardware & IoT §11 floor layout not yet supplied; displaying provisional simulation geometry"
            >
              PROVISIONAL SIMULATION LAYOUT ({widthM}m × {heightM}m)
            </span>
          )}
          <span className="c2-panel-meta">
            SELECTED: {state.selectedVehicleId}
          </span>
        </div>
      </div>

      <div className="c2-panel-body">
        {mapError && (
          <div className="c2-alert-box c2-alert-warning" role="status">
            Floor map endpoint notice: {mapError} (rendering provisional simulation fallback)
          </div>
        )}

        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: "6px",
            flexWrap: "wrap",
          }}
        >
          <div style={{ display: "flex", gap: "6px" }}>
            {(["SCOUT01", "HAWK01"] as const).map((vid) => {
              const v = state.vehicles[vid];
              const isSelected = state.selectedVehicleId === vid;
              return (
                <button
                  key={vid}
                  type="button"
                  className={`c2-btn ${isSelected ? "c2-btn-active" : "c2-btn-secondary"}`}
                  onClick={() => selectVehicle(vid)}
                  aria-pressed={isSelected}
                  aria-label={`Select ${vid} on tactical map`}
                >
                  {vid} [{getVehicleHealthLabel(v)}]
                </button>
              );
            })}
          </div>
          <span className="c2-panel-meta">
            GEOFENCE MAX ALT: {activeMap.hawk_geofence.max_altitude_m.toFixed(1)}m
          </span>
        </div>

        <svg
          viewBox={`0 0 ${MAP_VIEWBOX_WIDTH} ${MAP_VIEWBOX_HEIGHT}`}
          width="100%"
          height="360"
          role="img"
          aria-label="Indoor Floor Plan Tactical Display"
          style={{
            backgroundColor: "var(--bg-canvas)",
            border: "1px solid var(--border-subtle)",
            borderRadius: "2px",
            display: "block",
          }}
        >
          <defs>
            <pattern
              id="tactical-grid-pattern"
              width="30"
              height="30"
              patternUnits="userSpaceOnUse"
            >
              <path
                d="M 30 0 L 0 0 0 30"
                fill="none"
                stroke="#1A221E"
                strokeWidth="1"
              />
            </pattern>
          </defs>

          {/* Subtle Coordinate Grid Background */}
          <rect
            x={MAP_PADDING}
            y={MAP_PADDING}
            width={MAP_VIEWBOX_WIDTH - MAP_PADDING * 2}
            height={MAP_VIEWBOX_HEIGHT - MAP_PADDING * 2}
            fill="url(#tactical-grid-pattern)"
            stroke="#26302B"
            strokeWidth="1"
          />

          {/* Coordinate Axis Ticks */}
          {xTicks.map((tick) => {
            const sx = worldToSvgX(tick, widthM);
            return (
              <g key={`xtick-${tick}`}>
                <line
                  x1={sx}
                  y1={MAP_PADDING - 5}
                  x2={sx}
                  y2={MAP_PADDING}
                  stroke="#636E64"
                  strokeWidth="1"
                />
                <text
                  x={sx}
                  y={MAP_PADDING - 10}
                  fill="#9CA59B"
                  fontSize="10"
                  fontFamily="monospace"
                  textAnchor="middle"
                >
                  {tick}m
                </text>
              </g>
            );
          })}
          {yTicks.map((tick) => {
            const sy = worldToSvgY(tick, heightM);
            return (
              <g key={`ytick-${tick}`}>
                <line
                  x1={MAP_PADDING - 5}
                  y1={sy}
                  x2={MAP_PADDING}
                  y2={sy}
                  stroke="#636E64"
                  strokeWidth="1"
                />
                <text
                  x={MAP_PADDING - 8}
                  y={sy + 3}
                  fill="#9CA59B"
                  fontSize="10"
                  fontFamily="monospace"
                  textAnchor="end"
                >
                  {tick}m
                </text>
              </g>
            );
          })}

          {/* Indoor Rooms */}
          {activeMap.rooms.map((room) => {
            const r = worldRectToSvg(
              room.x,
              room.y,
              room.width,
              room.height,
              widthM,
              heightM,
            );
            return (
              <g key={room.id} data-testid={`map-room-${room.id}`}>
                <rect
                  x={r.x}
                  y={r.y}
                  width={r.width}
                  height={r.height}
                  fill="rgba(23, 30, 27, 0.35)"
                  stroke="#5C744D"
                  strokeWidth="1.5"
                />
                <text
                  x={r.x + 8}
                  y={r.y + 16}
                  fill="#9CA59B"
                  fontSize="10"
                  fontFamily="monospace"
                >
                  {room.name.toUpperCase()}
                </text>
              </g>
            );
          })}

          {/* HAWK01 Geofence Boundary */}
          <g data-testid="map-hawk-geofence">
            <rect
              x={geofenceRect.x}
              y={geofenceRect.y}
              width={geofenceRect.width}
              height={geofenceRect.height}
              fill="none"
              stroke="#D98A2B"
              strokeWidth="1.5"
              strokeDasharray="6 4"
            />
            <text
              x={geofenceRect.x + 8}
              y={geofenceRect.y + geofenceRect.height - 8}
              fill="#D98A2B"
              fontSize="10"
              fontFamily="monospace"
            >
              {activeMap.hawk_geofence.label.toUpperCase()}
            </text>
          </g>

          {/* Fixed Waypoints */}
          {activeMap.waypoints.map((wp) => {
            const wx = worldToSvgX(wp.x, widthM);
            const wy = worldToSvgY(wp.y, heightM);
            return (
              <g key={wp.id} data-testid={`map-waypoint-${wp.id}`}>
                <circle
                  cx={wx}
                  cy={wy}
                  r="4"
                  fill="#111614"
                  stroke="#8AA37B"
                  strokeWidth="1.5"
                />
                <text
                  x={wx + 7}
                  y={wy + 3}
                  fill="#8AA37B"
                  fontSize="10"
                  fontFamily="monospace"
                >
                  {wp.label} ({wp.x.toFixed(1)},{wp.y.toFixed(1)})
                </text>
              </g>
            );
          })}

          {/* Reported Observations */}
          {state.observations.map((obs) => {
            const ox = worldToSvgX(obs.location_x, widthM);
            const oy = worldToSvgY(obs.location_y, heightM);
            return (
              <g key={obs.id} data-testid={`map-observation-${obs.id}`}>
                <polygon
                  points={`${ox},${oy - 6} ${ox + 6},${oy + 5} ${ox - 6},${oy + 5}`}
                  fill="#D98A2B"
                  stroke="#0A0D0C"
                  strokeWidth="1"
                />
                <text
                  x={ox + 8}
                  y={oy + 3}
                  fill="#F3B35F"
                  fontSize="10"
                  fontFamily="monospace"
                >
                  OBS:{obs.obs_type} ({Math.round(obs.confidence * 100)}%)
                </text>
              </g>
            );
          })}

          {/* Vehicle Markers: SCOUT01 (ground diamond) & HAWK01 (aerial reticle) */}
          {(["SCOUT01", "HAWK01"] as const).map((vid) => {
            const v = state.vehicles[vid];
            const vx = worldToSvgX(v.position.x, widthM);
            const vy = worldToSvgY(v.position.y, heightM);
            const isSelected = state.selectedVehicleId === vid;
            const isDegraded =
              v.connection !== "CONNECTED" ||
              v.status === "UNKNOWN" ||
              v.isStale;
            const isStopped =
              v.status === "EMERGENCY_STOPPED" || v.status === "FAULT";

            const strokeColor = isStopped
              ? "#E03E3E"
              : isDegraded
                ? "#D98A2B"
                : vid === "SCOUT01"
                  ? "#8AA37B"
                  : "#7A9A95";

            const healthTag = getVehicleHealthLabel(v);

            return (
              <g
                key={vid}
                data-testid={`map-marker-${vid}`}
                onClick={() => selectVehicle(vid)}
                style={{ cursor: "pointer" }}
                opacity={v.connection === "DISCONNECTED" ? 0.65 : 1}
              >
                {isSelected && (
                  <circle
                    cx={vx}
                    cy={vy}
                    r="16"
                    fill="none"
                    stroke="#E6E8E1"
                    strokeWidth="1"
                    strokeDasharray="3 2"
                    data-testid={`map-selected-ring-${vid}`}
                  />
                )}

                {vid === "SCOUT01" ? (
                  <polygon
                    points={`${vx},${vy - 9} ${vx + 9},${vy} ${vx},${vy + 9} ${vx - 9},${vy}`}
                    fill="#111614"
                    stroke={strokeColor}
                    strokeWidth="2"
                  />
                ) : (
                  <g>
                    <circle
                      cx={vx}
                      cy={vy}
                      r="8"
                      fill="#111614"
                      stroke={strokeColor}
                      strokeWidth="2"
                    />
                    <line
                      x1={vx - 12}
                      y1={vy}
                      x2={vx + 12}
                      y2={vy}
                      stroke={strokeColor}
                      strokeWidth="1.5"
                    />
                    <line
                      x1={vx}
                      y1={vy - 12}
                      x2={vx}
                      y2={vy + 12}
                      stroke={strokeColor}
                      strokeWidth="1.5"
                    />
                  </g>
                )}

                <text
                  x={vx + 13}
                  y={vy - 3}
                  fill="#E6E8E1"
                  fontSize="11"
                  fontWeight="700"
                  fontFamily="monospace"
                >
                  {vid} [{healthTag}]
                </text>
                <text
                  x={vx + 13}
                  y={vy + 10}
                  fill="#9CA59B"
                  fontSize="10"
                  fontFamily="monospace"
                >
                  X:{v.position.x.toFixed(2)}m Y:{v.position.y.toFixed(2)}m
                  {vid === "HAWK01" ? ` ALT:${v.position.alt.toFixed(2)}m` : ""}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
    </section>
  );
}
