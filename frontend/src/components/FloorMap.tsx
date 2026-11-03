import React, { useEffect, useRef, useState } from "react";
import { LidarScanView } from "./LidarScanView";
import {
  fetchFloorMap,
  uploadMapFiles,
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

export function isWithinMapBounds(
  xMeters: number,
  yMeters: number,
  widthMeters: number,
  heightMeters: number,
): boolean {
  return (
    xMeters >= 0 &&
    xMeters <= widthMeters &&
    yMeters >= 0 &&
    yMeters <= heightMeters
  );
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
    { id: "WP_INSPECT_A", label: "Inspect Alpha", x: 5.0, y: 4.0 },
    { id: "WP_INSPECT_B", label: "Inspect Bravo", x: 15.0, y: 8.0 },
  ],
  hawk_geofence: {
    id: "GEOFENCE_HAWK01",
    label: "HAWK01 Flight Envelope (Simulated / Roadmap, Not Integrated)",
    x: 1,
    y: 1,
    width: 18,
    height: 13,
    max_altitude_m: 3.0,
  },
};

export function FloorMap(): React.JSX.Element {
  const { state, dispatch } = useDashboardStore();
  const [mapConfig, setMapConfig] =
    useState<FloorPlanConfigPayload>(FALLBACK_SIMULATION_MAP);
  const [mapError, setMapError] = useState<string | null>(null);
  const [zoomLevel, setZoomLevel] = useState<number>(1.0);
  const [showTrail, setShowTrail] = useState<boolean>(true);
  const [activeLayer, setActiveLayer] = useState<"floor" | "lidar">("floor");
  const [selectedWaypointId, setSelectedWaypointId] = useState<string | null>(null);
  const [scoutBreadcrumbs, setScoutBreadcrumbs] = useState<Array<{ x: number; y: number }>>([]);
  const [pendingMapFiles, setPendingMapFiles] = useState<File[]>([]);
  const [isUploadingMap, setIsUploadingMap] = useState(false);
  const [mapUploadNotice, setMapUploadNotice] = useState<string | null>(null);
  const [mapUploadError, setMapUploadError] = useState<string | null>(null);
  const mapFileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let cancelled = false;
    fetchFloorMap()
      .then((payload) => {
        if (!cancelled) {
          setMapConfig(payload);
          dispatch({ type: "SET_FLOOR_PLAN", payload });
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setMapError(
            err instanceof Error ? err.message : "Failed to load floor map",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [dispatch]);

  // Update SCOUT01 breadcrumbs trail only when vehicle is actively connected and fresh
  useEffect(() => {
    const scout = state.vehicles.SCOUT01;
    if (scout.connection !== "CONNECTED" || scout.isStale) {
      return;
    }
    const pos = scout.position;
    setScoutBreadcrumbs((prev) => {
      const last = prev[prev.length - 1];
      if (last && Math.hypot(last.x - pos.x, last.y - pos.y) < 0.2) {
        return prev;
      }
      return [...prev.slice(-24), { x: pos.x, y: pos.y }];
    });
  }, [
    state.vehicles.SCOUT01.position,
    state.vehicles.SCOUT01.connection,
    state.vehicles.SCOUT01.isStale,
  ]);

  const activeMap = mapConfig;
  const widthM = activeMap.width_m > 0 ? activeMap.width_m : 20;
  const heightM = activeMap.height_m > 0 ? activeMap.height_m : 15;

  const xTicks = [0, 5, 10, 15, 20].filter((v) => v <= widthM);
  const yTicks = [0, 5, 10, 15].filter((v) => v <= heightM);

  const selectVehicle = (vehicleId: VehicleId): void => {
    dispatch({ type: "SELECT_VEHICLE", payload: vehicleId });
  };

  const selectedWaypoint = activeMap.waypoints.find((w) => w.id === selectedWaypointId);

  const handleArchiveMapFiles = async (): Promise<void> => {
    if (pendingMapFiles.length === 0 || isUploadingMap) return;
    const fileNames = pendingMapFiles.map((file) => file.name).join(", ");
    if (
      !window.confirm(
        `Archive ${fileNames} for SCOUT01? This may replace saved files with the same canonical names and does not change the live dashboard map.`,
      )
    ) {
      return;
    }

    setIsUploadingMap(true);
    setMapUploadError(null);
    setMapUploadNotice(null);
    try {
      const result = await uploadMapFiles("SCOUT01", pendingMapFiles);
      setMapUploadNotice(
        result.saved_files.length > 0
          ? `Archived: ${result.saved_files.join(", ")}`
          : "No supported map files were archived.",
      );
      setPendingMapFiles([]);
      if (mapFileInputRef.current) mapFileInputRef.current.value = "";
    } catch (err) {
      setMapUploadError(
        err instanceof Error ? err.message : "Map file upload failed.",
      );
    } finally {
      setIsUploadingMap(false);
    }
  };

  // Only plot live detections when frame === "map", mapProjectionAllowed is explicitly confirmed,
  // ROS connection is CONNECTED, and stream is NOT stale.
  // Camera-relative (`oak_rgb_camera_optical_frame`, `oak_parent_frame`), unknown frames, and
  // unconfirmed `map` frames are strictly excluded from the global floor map.
  const scoutDetections = state.detections.SCOUT01;
  const canRenderMapDetections =
    scoutDetections.hasReceivedFrame &&
    !scoutDetections.isStale &&
    scoutDetections.rosConnectionState === "CONNECTED" &&
    scoutDetections.frame === "map" &&
    scoutDetections.mapProjectionAllowed;

  const inBoundsMapDetections = canRenderMapDetections
    ? scoutDetections.objects.filter((obj) =>
        isWithinMapBounds(obj.position.x, obj.position.y, widthM, heightM),
      )
    : [];

  const outOfBoundsMapDetections = canRenderMapDetections
    ? scoutDetections.objects.filter(
        (obj) =>
          !isWithinMapBounds(obj.position.x, obj.position.y, widthM, heightM),
      )
    : [];

  return (
    <section className="center-map" aria-label="SVG Floor Map">
      <div className="map-header">
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <h2 className="panel-title">
            {activeLayer === "floor"
              ? `OPERATIONS MAP // ${activeMap.coordinate_system.toUpperCase()}`
              : "SCOUT01 // LIVE 2D SLAM PROJECTION"}
          </h2>
          {activeLayer === "floor" && activeMap.is_provisional_simulation_config && (
            <span
              className="badge badge-warning"
              style={{ fontSize: "9px", padding: "1px 6px" }}
              title="Provisional simulation geometry"
            >
              PROVISIONAL SIMULATION LAYOUT ({widthM}m × {heightM}m)
            </span>
          )}
        </div>

        <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
          <div className="map-layer-toggle" aria-label="Map display layer">
            <button
              type="button"
              className={`btn ${activeLayer === "floor" ? "btn-primary" : "btn-secondary"}`}
              aria-pressed={activeLayer === "floor"}
              onClick={() => setActiveLayer("floor")}
            >
              FLOOR PLAN
            </button>
            <button
              type="button"
              className={`btn ${activeLayer === "lidar" ? "btn-primary" : "btn-secondary"}`}
              aria-pressed={activeLayer === "lidar"}
              onClick={() => setActiveLayer("lidar")}
            >
              LIVE 2D MAP
            </button>
          </div>
          {(["SCOUT01"] as const).map((vid) => {
            const v = state.vehicles[vid];
            const isSelected = state.selectedVehicleId === vid;
            return (
              <button
                key={vid}
                type="button"
                className={`btn ${isSelected ? "btn-primary" : "btn-secondary"}`}
                style={{ padding: "2px 8px", fontSize: "10px" }}
                onClick={() => selectVehicle(vid)}
                aria-pressed={isSelected}
                aria-label={`Select ${vid} on tactical map`}
              >
                {vid} [{getVehicleHealthLabel(v)}]
              </button>
            );
          })}
          <span className="panel-meta" style={{ marginLeft: "4px" }}>
            FOCUSED: {state.selectedVehicleId}
          </span>
        </div>
      </div>

      <div className="map-viewport">
        {activeLayer === "floor" && mapError && (
          <div
            className="c2-alert-box c2-alert-warn"
            role="status"
            style={{ position: "absolute", top: "10px", left: "10px", zIndex: 10 }}
          >
            Floor map notice: {mapError} (using fallback geometry)
          </div>
        )}

        {outOfBoundsMapDetections.length > 0 && (
          <div
            className="c2-alert-box c2-alert-warn"
            role="status"
            data-testid="map-out-of-bounds-detections-warning"
            style={{ position: "absolute", top: "10px", left: "10px", zIndex: 10 }}
          >
            MAP BOUNDARY NOTICE: {outOfBoundsMapDetections.length} map-frame
            detection(s) outside [{widthM}m × {heightM}m] bounds omitted without
            clamping (
            {outOfBoundsMapDetections
              .map(
                (o) =>
                  `${o.class}#${o.id}@(${o.position.x.toFixed(2)}m,${o.position.y.toFixed(2)}m)`,
              )
              .join(", ")}
            )
          </div>
        )}

        {/* Floating Zoom & Layer Controls */}
        {activeLayer === "floor" && <div className="map-toolbar">
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => setZoomLevel((z) => Math.min(2.0, Number((z + 0.2).toFixed(1))))}
            aria-label="Zoom Map In"
            title="Zoom In"
          >
            +
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => setZoomLevel((z) => Math.max(0.8, Number((z - 0.2).toFixed(1))))}
            aria-label="Zoom Map Out"
            title="Zoom Out"
          >
            -
          </button>
          <button
            type="button"
            className="btn btn-secondary"
            style={{ fontSize: "9px", padding: "4px 4px" }}
            onClick={() => setZoomLevel(1.0)}
            aria-label="Reset Map Zoom"
            title="Reset Zoom"
          >
            1:1
          </button>
          <button
            type="button"
            className={`btn ${showTrail ? "btn-primary" : "btn-secondary"}`}
            style={{ fontSize: "9px", padding: "4px 4px" }}
            onClick={() => setShowTrail((prev) => !prev)}
            aria-label="Toggle Vehicle Trail"
            title="Toggle Breadcrumb Trail"
          >
            TRL
          </button>
          <input
            ref={mapFileInputRef}
            type="file"
            accept=".ply,.pcd,.db"
            multiple
            hidden
            aria-label="Select SCOUT01 map files"
            onChange={(event) => {
              const files = Array.from(event.currentTarget.files ?? []);
              setPendingMapFiles(files);
              setMapUploadNotice(null);
              setMapUploadError(null);
            }}
          />
          <button
            type="button"
            className="btn btn-secondary"
            style={{ fontSize: "8px", padding: "4px" }}
            onClick={() => {
              if (pendingMapFiles.length > 0) {
                void handleArchiveMapFiles();
              } else {
                mapFileInputRef.current?.click();
              }
            }}
            disabled={isUploadingMap}
            aria-label={
              pendingMapFiles.length > 0
                ? `Archive ${pendingMapFiles.length} selected map files`
                : "Select map files to archive"
            }
            title="Archive .ply, .pcd, or .db files on the backend; does not change the live map."
          >
            {isUploadingMap
              ? "SAVING..."
              : pendingMapFiles.length > 0
                ? `SAVE ${pendingMapFiles.length}`
                : "ARCHIVE MAP"}
          </button>
        </div>}
        {(mapUploadNotice || mapUploadError) && (
          <div
            className={`c2-alert-box ${mapUploadError ? "c2-alert-error" : "c2-alert-info"}`}
            role={mapUploadError ? "alert" : "status"}
            style={{ position: "absolute", bottom: "8px", left: "8px", zIndex: 5 }}
          >
            {mapUploadError ?? mapUploadNotice}
          </div>
        )}

        {/* Selected Waypoint HUD Card */}
        {activeLayer === "floor" && selectedWaypoint && (
          <div className="waypoint-hud-card">
            <div style={{ color: "var(--marker-scout)", fontWeight: 600 }}>
              {selectedWaypoint.label} [{selectedWaypoint.id}]
            </div>
            <div style={{ color: "var(--text-secondary)", fontSize: "10px", marginTop: "2px" }}>
              X: {selectedWaypoint.x.toFixed(2)}m  Y: {selectedWaypoint.y.toFixed(2)}m
            </div>
          </div>
        )}

        {activeLayer === "lidar" ? <LidarScanView /> : (
        /* Main Tactical SVG Canvas */
        <svg
          viewBox={`0 0 ${MAP_VIEWBOX_WIDTH} ${MAP_VIEWBOX_HEIGHT}`}
          width="100%"
          height="100%"
          role="img"
          aria-label="Indoor Floor Plan Tactical Display"
          style={{
            display: "block",
            transform: `scale(${zoomLevel})`,
            transformOrigin: "center center",
            transition: "transform 150ms ease-out",
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
                stroke="#14171A"
                strokeWidth="1"
              />
            </pattern>
          </defs>

          {/* Coordinate Grid Background */}
          <rect
            x={MAP_PADDING}
            y={MAP_PADDING}
            width={MAP_VIEWBOX_WIDTH - MAP_PADDING * 2}
            height={MAP_VIEWBOX_HEIGHT - MAP_PADDING * 2}
            fill="url(#tactical-grid-pattern)"
            stroke="#1E2226"
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
                  stroke="#4E5553"
                  strokeWidth="1"
                />
                <text
                  x={sx}
                  y={MAP_PADDING - 8}
                  fill="#8A8F8C"
                  fontSize="9"
                  fontFamily="var(--font-data), monospace"
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
                  stroke="#4E5553"
                  strokeWidth="1"
                />
                <text
                  x={MAP_PADDING - 8}
                  y={sy + 3}
                  fill="#8A8F8C"
                  fontSize="9"
                  fontFamily="var(--font-data), monospace"
                  textAnchor="end"
                >
                  {tick}m
                </text>
              </g>
            );
          })}

          {/* Defined Rooms / Sectors */}
          {activeMap.rooms.map((room) => {
            const rect = worldRectToSvg(
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
                  x={rect.x}
                  y={rect.y}
                  width={rect.width}
                  height={rect.height}
                  fill="#0D0F10"
                  fillOpacity="0.75"
                  stroke="#2A3038"
                  strokeWidth="1.2"
                />
                <text
                  x={rect.x + 8}
                  y={rect.y + 16}
                  fill="#8A8F8C"
                  fontSize="10"
                  fontFamily="var(--font-ui), sans-serif"
                  fontWeight="600"
                  letterSpacing="0.04em"
                >
                  {room.name.toUpperCase()}
                </text>
              </g>
            );
          })}

          {/* Waypoints */}
          {activeMap.waypoints.map((wp) => {
            const wx = worldToSvgX(wp.x, widthM);
            const wy = worldToSvgY(wp.y, heightM);
            const isSelectedWp = selectedWaypointId === wp.id;
            return (
              <g
                key={wp.id}
                data-testid={`map-waypoint-${wp.id}`}
                onClick={() => setSelectedWaypointId(isSelectedWp ? null : wp.id)}
                style={{ cursor: "pointer" }}
              >
                {isSelectedWp && (
                  <circle
                    cx={wx}
                    cy={wy}
                    r="9"
                    fill="none"
                    stroke="#E2E4DE"
                    strokeWidth="1"
                    strokeDasharray="2 2"
                  />
                )}
                <circle
                  cx={wx}
                  cy={wy}
                  r="4"
                  fill="#131618"
                  stroke={isSelectedWp ? "#E2E4DE" : "#5A8A6A"}
                  strokeWidth="1.5"
                />
                <text
                  x={wx + 7}
                  y={wy + 3}
                  fill={isSelectedWp ? "#E2E4DE" : "#5A8A6A"}
                  fontSize="10"
                  fontFamily="var(--font-data), monospace"
                >
                  {wp.label} ({wp.x.toFixed(1)},{wp.y.toFixed(1)})
                </text>
              </g>
            );
          })}

          {/* Observations */}
          {state.observations.map((obs) => {
            const ox = worldToSvgX(obs.location_x, widthM);
            const oy = worldToSvgY(obs.location_y, heightM);
            return (
              <g key={obs.id} data-testid={`map-observation-${obs.id}`}>
                <polygon
                  points={`${ox},${oy - 6} ${ox + 6},${oy + 5} ${ox - 6},${oy + 5}`}
                  fill="#C8882A"
                  stroke="#08090A"
                  strokeWidth="1"
                />
                <text
                  x={ox + 8}
                  y={oy + 3}
                  fill="#D8A850"
                  fontSize="10"
                  fontFamily="var(--font-data), monospace"
                >
                  OBS:{obs.obs_type} ({Math.round(obs.confidence * 100)}%)
                </text>
              </g>
            );
          })}

          {/* Live ROS Map-Frame Object Detections (Distinct Target Reticle Markers) */}
          {inBoundsMapDetections.map((detObj) => {
            const dx = worldToSvgX(detObj.position.x, widthM);
            const dy = worldToSvgY(detObj.position.y, heightM);
            const confPct = Math.round(detObj.score * 100);
            return (
              <g
                key={`ros-det-${detObj.id}-${detObj.class}`}
                data-testid={`map-detection-marker-${detObj.id}`}
              >
                <rect
                  x={dx - 6}
                  y={dy - 6}
                  width="12"
                  height="12"
                  fill="#1F180B"
                  stroke="#D98A2B"
                  strokeWidth="1.5"
                />
                <circle cx={dx} cy={dy} r="2.5" fill="#E6E8E1" />
                <text
                  x={dx + 9}
                  y={dy + 3}
                  fill="#E6E8E1"
                  fontSize="10"
                  fontFamily="var(--font-data), monospace"
                >
                  DET:{detObj.class.toUpperCase()} #{detObj.id} ({confPct}%)
                </text>
              </g>
            );
          })}

          {/* Breadcrumb Trail */}
          {showTrail && scoutBreadcrumbs.length > 1 && (
            <polyline
              points={scoutBreadcrumbs
                .map((pt) => `${worldToSvgX(pt.x, widthM)},${worldToSvgY(pt.y, heightM)}`)
                .join(" ")}
              fill="none"
              stroke="#5A8A6A"
              strokeWidth="1.5"
              strokeDasharray="3 3"
              opacity="0.6"
            />
          )}

          {/* Vehicle Markers */}
          {(["SCOUT01"] as const).map((vid) => {
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
              ? "#E04040"
              : isDegraded
                ? "#C8882A"
                : vid === "SCOUT01"
                  ? "#7AAA8A"
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
                {/* Radar sweep animation */}
                {v.status === "ACTIVE" && !isStopped && (
                  <circle
                    cx={vx}
                    cy={vy}
                    r="8"
                    className="radar-ring c2-radar-ring"
                    fill="none"
                    stroke={strokeColor}
                    strokeWidth="1.5"
                  />
                )}

                {isSelected && (
                  <circle
                    cx={vx}
                    cy={vy}
                    r="16"
                    fill="none"
                    stroke="#E2E4DE"
                    strokeWidth="1"
                    strokeDasharray="3 2"
                    data-testid={`map-selected-ring-${vid}`}
                  />
                )}

                {vid === "SCOUT01" ? (
                  <g>
                    {/* Heading Vector */}
                    <line
                      x1={vx}
                      y1={vy}
                      x2={vx + 12}
                      y2={vy - 12}
                      stroke="#7AAA8A"
                      strokeWidth="2"
                      strokeLinecap="round"
                    />
                    <polygon
                      points={`${vx},${vy - 8} ${vx + 8},${vy} ${vx},${vy + 8} ${vx - 8},${vy}`}
                      fill="#131618"
                      stroke={strokeColor}
                      strokeWidth="2"
                    />
                  </g>
                ) : (
                  <g>
                    <circle
                      cx={vx}
                      cy={vy}
                      r="8"
                      fill="#131618"
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

                {/* Marker Callout Tag */}
                <rect
                  x={vx + 11}
                  y={vy - 16}
                  width="80"
                  height="26"
                  fill="#0D0F10"
                  fillOpacity="0.88"
                  stroke={strokeColor}
                  strokeWidth="1"
                  rx="2"
                />
                <text
                  x={vx + 15}
                  y={vy - 5}
                  fill="#E2E4DE"
                  fontSize="10"
                  fontWeight="600"
                  fontFamily="var(--font-data), monospace"
                >
                  {vid}
                </text>
                <text
                  x={vx + 15}
                  y={vy + 6}
                  fill={strokeColor}
                  fontSize="8"
                  fontFamily="var(--font-data), monospace"
                >
                  {healthTag} // {v.battery.toFixed(0)}%
                </text>
              </g>
            );
          })}
        </svg>
        )}
      </div>
    </section>
  );
}
