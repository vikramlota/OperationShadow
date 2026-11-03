import React, { useEffect, useRef, useState } from "react";
import { ROSBRIDGE_URL } from "../api/client";

const MAP_TOPIC = "/cloud_registered";
const UNILIDAR_TOPIC = "/unilidar/cloud";
const MAP_TYPE = "sensor_msgs/msg/PointCloud2";
const PATH_TOPIC = "/path";
const PATH_TYPE = "nav_msgs/msg/Path";
const RECONNECT_DELAY_MS = 2000;
const MAX_RENDERED_POINTS_PER_FRAME = 2400;
const MAX_ACCUMULATED_POINTS = 6000;
const VOXEL_SIZE_METERS = 0.08;
const SVG_WIDTH = 680;
const SVG_HEIGHT = 480;
const PADDING = 38;

interface Point2D {
  x: number;
  y: number;
}

interface PositionStamped {
  pose?: {
    position?: {
      x?: unknown;
      y?: unknown;
    };
  };
}

interface PointField {
  name?: unknown;
  offset?: unknown;
  datatype?: unknown;
}

interface PointCloud2Message {
  header?: {
    frame_id?: unknown;
    stamp?: {
      sec?: unknown;
      nanosec?: unknown;
    };
  };
  height?: unknown;
  width?: unknown;
  fields?: unknown;
  is_bigendian?: unknown;
  point_step?: unknown;
  row_step?: unknown;
  data?: unknown;
}

type LidarStatus = "connecting" | "live" | "reconnecting" | "error";

interface LidarScan {
  points: Point2D[];
  frameId: string;
  pointCount: number;
  stamp: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function decodePointCloud(cloud: PointCloud2Message): LidarScan {
  if (
    !Array.isArray(cloud.fields) ||
    typeof cloud.data !== "string" ||
    typeof cloud.width !== "number" ||
    typeof cloud.height !== "number" ||
    typeof cloud.point_step !== "number" ||
    typeof cloud.row_step !== "number" ||
    cloud.width < 1 ||
    cloud.height < 1 ||
    cloud.point_step < 12 ||
    cloud.row_step < cloud.width * cloud.point_step
  ) {
    throw new Error("Invalid PointCloud2 layout");
  }

  const fields = cloud.fields as PointField[];
  const xField = fields.find((field) => field.name === "x");
  const yField = fields.find((field) => field.name === "y");
  const zField = fields.find((field) => field.name === "z");
  if (
    typeof xField?.offset !== "number" ||
    typeof yField?.offset !== "number" ||
    typeof zField?.offset !== "number" ||
    xField.datatype !== 7 ||
    yField.datatype !== 7 ||
    zField.datatype !== 7
  ) {
    throw new Error("PointCloud2 must contain FLOAT32 x/y/z fields");
  }

  const binary = atob(cloud.data);
  if (binary.length < cloud.row_step * cloud.height) {
    throw new Error("PointCloud2 data is shorter than its declared dimensions");
  }

  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  const view = new DataView(bytes.buffer);
  const littleEndian = cloud.is_bigendian !== true;
  const pointCount = cloud.width * cloud.height;
  const sampleStride = Math.max(
    1,
    Math.ceil(pointCount / MAX_RENDERED_POINTS_PER_FRAME),
  );
  const points: Point2D[] = [];
  let pointIndex = 0;

  for (let row = 0; row < cloud.height; row += 1) {
    const rowStart = row * cloud.row_step;
    for (let column = 0; column < cloud.width; column += 1) {
      if (pointIndex % sampleStride === 0) {
        const pointStart = rowStart + column * cloud.point_step;
        const x = view.getFloat32(pointStart + xField.offset, littleEndian);
        const y = view.getFloat32(pointStart + yField.offset, littleEndian);
        const z = view.getFloat32(pointStart + zField.offset, littleEndian);
        if (
          Number.isFinite(x) &&
          Number.isFinite(y) &&
          Number.isFinite(z) &&
          Math.abs(x) <= 100 &&
          Math.abs(y) <= 100
        ) {
          points.push({ x, y });
        }
      }
      pointIndex += 1;
    }
  }

  const seconds = cloud.header?.stamp?.sec;
  const nanoseconds = cloud.header?.stamp?.nanosec;
  const stamp =
    typeof seconds === "number" && typeof nanoseconds === "number"
      ? new Date(seconds * 1000 + nanoseconds / 1_000_000).toISOString()
      : null;

  return {
    points,
    frameId:
      typeof cloud.header?.frame_id === "string"
        ? cloud.header.frame_id
        : "unknown",
    pointCount,
    stamp,
  };
}

function getVoxelKey(point: Point2D): string {
  return `${Math.floor(point.x / VOXEL_SIZE_METERS)}:${Math.floor(point.y / VOXEL_SIZE_METERS)}`;
}

function generateDemoLidarScan(): LidarScan {
  const points: Point2D[] = [];
  // Outer perimeter room walls (Sector Alpha bay in camera_init frame)
  for (let x = -4.5; x <= 4.5; x += 0.09) {
    points.push({ x: Number(x.toFixed(2)), y: -3.2 });
    points.push({ x: Number(x.toFixed(2)), y: 3.2 });
  }
  for (let y = -3.2; y <= 3.2; y += 0.09) {
    if (Math.abs(y) > 0.8) {
      points.push({ x: -4.5, y: Number(y.toFixed(2)) });
    }
    points.push({ x: 4.5, y: Number(y.toFixed(2)) });
  }
  // Obstacle cluster 1 (pillar / barrier)
  for (let x = 1.0; x <= 1.8; x += 0.09) {
    for (let y = 0.6; y <= 1.4; y += 0.09) {
      points.push({ x: Number(x.toFixed(2)), y: Number(y.toFixed(2)) });
    }
  }
  // Obstacle cluster 2 (corridor corner)
  for (let x = -2.2; x <= -1.4; x += 0.09) {
    for (let y = 1.2; y <= 2.0; y += 0.09) {
      points.push({ x: Number(x.toFixed(2)), y: Number(y.toFixed(2)) });
    }
  }
  return {
    points,
    frameId: "camera_init",
    pointCount: points.length * 4,
    stamp: new Date().toISOString(),
  };
}

const DEMO_TRAJECTORY: Point2D[] = [
  { x: -3.5, y: -2.0 },
  { x: -2.8, y: -1.6 },
  { x: -2.0, y: -1.0 },
  { x: -1.2, y: -0.4 },
  { x: -0.3, y: 0.1 },
  { x: 0.5, y: 0.0 },
  { x: 1.2, y: -0.6 },
  { x: 2.0, y: -0.2 },
  { x: 2.6, y: 0.8 },
  { x: 2.2, y: 1.8 },
];

export function LidarScanView(): React.JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [map, setMap] = useState<LidarScan | null>(null);
  const [trajectory, setTrajectory] = useState<Point2D[]>([]);
  const [status, setStatus] = useState<LidarStatus>("connecting");

  const loadDemoScan = (): void => {
    setMap(generateDemoLidarScan());
    setTrajectory(DEMO_TRAJECTORY);
    setStatus("live");
  };

  useEffect(() => {
    let active = true;
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let staleTimer: ReturnType<typeof setTimeout> | undefined;
    let hasConnected = false;

    const connect = (): void => {
      if (!active) return;
      setStatus(hasConnected ? "reconnecting" : "connecting");

      try {
        socket = new WebSocket(ROSBRIDGE_URL);
      } catch {
        setStatus("error");
        reconnectTimer = setTimeout(connect, RECONNECT_DELAY_MS);
        return;
      }

      socket.onopen = () => {
        if (!active || !socket) return;
        hasConnected = true;
        setStatus("connecting");
        socket.send(
          JSON.stringify({
            op: "subscribe",
            id: "operation-shadow-scout-lidar",
            topic: MAP_TOPIC,
            type: MAP_TYPE,
            throttle_rate: 200,
            queue_length: 1,
          }),
        );
        socket.send(
          JSON.stringify({
            op: "subscribe",
            id: "operation-shadow-scout-path",
            topic: PATH_TOPIC,
            type: PATH_TYPE,
            throttle_rate: 500,
            queue_length: 1,
          }),
        );
        socket.send(
          JSON.stringify({
            op: "subscribe",
            id: "operation-shadow-scout-unilidar",
            topic: UNILIDAR_TOPIC,
            type: MAP_TYPE,
            throttle_rate: 250,
            queue_length: 1,
          }),
        );
      };

      socket.onmessage = (event: MessageEvent<string>) => {
        if (!active) return;
        try {
          const message: unknown = JSON.parse(event.data);
          if (!isRecord(message)) return;
          if (message.op !== "publish") return;
          if (
            (message.topic === MAP_TOPIC || message.topic === UNILIDAR_TOPIC) &&
            isRecord(message.msg)
          ) {
            const frame = decodePointCloud(message.msg as PointCloud2Message);
            setMap((previous) => {
              const accumulated = new Map<string, Point2D>();
              previous?.points.forEach((point) =>
                accumulated.set(getVoxelKey(point), point),
              );
              frame.points.forEach((point) =>
                accumulated.set(getVoxelKey(point), point),
              );
              while (accumulated.size > MAX_ACCUMULATED_POINTS) {
                const oldest = accumulated.keys().next().value;
                if (oldest === undefined) break;
                accumulated.delete(oldest);
              }
              return {
                ...frame,
                points: Array.from(accumulated.values()),
              };
            });
            setStatus("live");
            if (staleTimer) clearTimeout(staleTimer);
            staleTimer = setTimeout(() => {
              if (active) setStatus("reconnecting");
            }, 3000);
            return;
          }

          if (message.topic === PATH_TOPIC && isRecord(message.msg)) {
            const poses = message.msg.poses;
            if (!Array.isArray(poses)) return;
            const pathPoints = (poses as PositionStamped[])
              .map((item) => ({
                x: item.pose?.position?.x,
                y: item.pose?.position?.y,
              }))
              .filter(
                (point): point is { x: number; y: number } =>
                  typeof point.x === "number" &&
                  typeof point.y === "number" &&
                  Number.isFinite(point.x) &&
                  Number.isFinite(point.y),
              );
            setTrajectory(pathPoints.slice(-2000));
          }
        } catch {
          setStatus("error");
        }
      };

      socket.onerror = () => {
        if (active) setStatus("error");
      };

      socket.onclose = () => {
        if (!active) return;
        if (staleTimer) clearTimeout(staleTimer);
        setStatus("reconnecting");
        reconnectTimer = setTimeout(connect, RECONNECT_DELAY_MS);
      };
    };

    connect();
    return () => {
      active = false;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (staleTimer) clearTimeout(staleTimer);
      socket?.close();
    };
  }, []);

  const bounds = map?.points.reduce(
    (result, point) => ({
      minX: Math.min(result.minX, point.x),
      maxX: Math.max(result.maxX, point.x),
      minY: Math.min(result.minY, point.y),
      maxY: Math.max(result.maxY, point.y),
    }),
    {
      minX: Number.POSITIVE_INFINITY,
      maxX: Number.NEGATIVE_INFINITY,
      minY: Number.POSITIVE_INFINITY,
      maxY: Number.NEGATIVE_INFINITY,
    },
  );
  const centerX =
    bounds && Number.isFinite(bounds.minX) ? (bounds.minX + bounds.maxX) / 2 : 0;
  const centerY =
    bounds && Number.isFinite(bounds.minY) ? (bounds.minY + bounds.maxY) / 2 : 0;
  const span = Math.max(
    bounds ? bounds.maxX - bounds.minX : 0,
    bounds ? bounds.maxY - bounds.minY : 0,
    2,
  );
  const scale = (Math.min(SVG_WIDTH, SVG_HEIGHT) - PADDING * 2) / span;
  const toSvgX = (x: number): number => SVG_WIDTH / 2 + (x - centerX) * scale;
  const toSvgY = (y: number): number => SVG_HEIGHT / 2 - (y - centerY) * scale;
  const gridRadius = Math.max(1, Math.floor(span / 2));
  const statusLabel =
    status === "live"
      ? "LIVE"
      : status === "error"
        ? "FEED ERROR"
        : status === "reconnecting"
          ? "RECONNECTING"
          : "CONNECTING";

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const draw = (): void => {
      const boundsRect = canvas.getBoundingClientRect();
      if (boundsRect.width === 0 || boundsRect.height === 0) return;

      const pixelRatio = window.devicePixelRatio || 1;
      canvas.width = Math.round(boundsRect.width * pixelRatio);
      canvas.height = Math.round(boundsRect.height * pixelRatio);
      const context = canvas.getContext("2d");
      if (!context) return;

      context.setTransform(1, 0, 0, 1, 0, 0);
      context.fillStyle = "#08090A";
      context.fillRect(0, 0, canvas.width, canvas.height);
      const renderScale = Math.min(
        canvas.width / SVG_WIDTH,
        canvas.height / SVG_HEIGHT,
      );
      const offsetX = (canvas.width - SVG_WIDTH * renderScale) / 2;
      const offsetY = (canvas.height - SVG_HEIGHT * renderScale) / 2;
      context.setTransform(renderScale, 0, 0, renderScale, offsetX, offsetY);

      context.strokeStyle = "#14171A";
      context.lineWidth = 1;
      for (let x = PADDING; x <= SVG_WIDTH - PADDING; x += 32) {
        context.beginPath();
        context.moveTo(x, PADDING);
        context.lineTo(x, SVG_HEIGHT - PADDING);
        context.stroke();
      }
      for (let y = PADDING; y <= SVG_HEIGHT - PADDING; y += 32) {
        context.beginPath();
        context.moveTo(PADDING, y);
        context.lineTo(SVG_WIDTH - PADDING, y);
        context.stroke();
      }
      context.strokeStyle = "#25302B";
      context.setLineDash([3, 5]);
      for (let index = 1; index <= gridRadius; index += 1) {
        const radius = index * 2 * scale;
        if (radius > SVG_WIDTH / 2 - PADDING) break;
        context.beginPath();
        context.arc(SVG_WIDTH / 2, SVG_HEIGHT / 2, radius, 0, Math.PI * 2);
        context.stroke();
      }
      context.setLineDash([]);

      context.strokeStyle = "#3B4540";
      context.beginPath();
      context.moveTo(PADDING, SVG_HEIGHT / 2);
      context.lineTo(SVG_WIDTH - PADDING, SVG_HEIGHT / 2);
      context.moveTo(SVG_WIDTH / 2, PADDING);
      context.lineTo(SVG_WIDTH / 2, SVG_HEIGHT - PADDING);
      context.stroke();

      context.fillStyle = "#A5C58F";
      for (const point of map?.points ?? []) {
        context.beginPath();
        context.arc(toSvgX(point.x), toSvgY(point.y), 1.35, 0, Math.PI * 2);
        context.fill();
      }

      if (trajectory.length > 1) {
        context.strokeStyle = "#D98A2B";
        context.lineWidth = 2;
        context.setLineDash([5, 3]);
        context.beginPath();
        trajectory.forEach((point, index) => {
          const x = toSvgX(point.x);
          const y = toSvgY(point.y);
          if (index === 0) context.moveTo(x, y);
          else context.lineTo(x, y);
        });
        context.stroke();
        context.setLineDash([]);
      }

      const currentPose = trajectory[trajectory.length - 1];
      const poseX = currentPose ? toSvgX(currentPose.x) : SVG_WIDTH / 2;
      const poseY = currentPose ? toSvgY(currentPose.y) : SVG_HEIGHT / 2;
      context.fillStyle = "#0D0F10";
      context.strokeStyle = "#E2E4DE";
      context.lineWidth = 1.5;
      context.beginPath();
      context.arc(poseX, poseY, 5, 0, Math.PI * 2);
      context.fill();
      context.stroke();
      context.fillStyle = "#E2E4DE";
      context.font = "10px monospace";
      context.fillText(
        map?.frameId === "camera_init" ? "SCOUT01 / SLAM" : "SCOUT01",
        poseX + 9,
        poseY - 8,
      );
      context.fillStyle = "#8A8F8C";
      context.textAlign = "right";
      context.fillText(
        "REGISTERED XY PROJECTION // NOT A NAVIGATION OCCUPANCY GRID",
        SVG_WIDTH - PADDING,
        SVG_HEIGHT - 12,
      );
      context.textAlign = "left";
    };

    draw();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [gridRadius, map, scale, toSvgX, toSvgY, trajectory]);

  return (
    <div className="lidar-scan-view" aria-label="Live 2D SLAM map">
      <div className="lidar-scan-meta">
        <span className={`badge ${status === "live" ? "badge-nominal" : "badge-warning"}`}>
          {statusLabel}
        </span>
        <span>
          {map
            ? `PTS ${map.pointCount.toLocaleString()} / VOXELS ${map.points.length.toLocaleString()}`
            : "WAITING FOR REGISTERED POINT CLOUD"}
        </span>
        <span>FRAME {map?.frameId ?? "—"}</span>
        <span>ROS: cloud_registered + path</span>
        <button
          type="button"
          className="btn btn-secondary"
          style={{ fontSize: "9px", padding: "1px 6px", height: "18px", lineHeight: "1" }}
          onClick={loadDemoScan}
          aria-label="Load Demo 2D SLAM Scan"
        >
          DEMO SLAM MAP
        </button>
        <span>XY VISUAL ONLY // NO NAVIGATION</span>
      </div>
      <canvas
        ref={canvasRef}
        className="lidar-scan-canvas"
        width={SVG_WIDTH}
        height={SVG_HEIGHT}
        role="img"
        aria-label="SCOUT01 registered LiDAR point cloud and SLAM trajectory"
        data-testid="lidar-scan-canvas"
      />
      {!map && (
        <div className="lidar-scan-empty" role="status" style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "6px" }}>
          <span>
            {status === "error"
              ? "Unable to decode registered PointCloud2 data. Check ROSBridge feed and point fields."
              : `Subscribing read-only to ${MAP_TOPIC} and ${PATH_TOPIC} at ${ROSBRIDGE_URL}`}
          </span>
          <button
            type="button"
            className="btn btn-primary"
            style={{ fontSize: "10px", padding: "4px 10px", marginTop: "4px" }}
            onClick={loadDemoScan}
          >
            LOAD DEMO 2D SLAM MAP
          </button>
        </div>
      )}
    </div>
  );
}
