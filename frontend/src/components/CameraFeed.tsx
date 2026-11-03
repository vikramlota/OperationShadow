import React, { useEffect, useState } from "react";
import {
  DEFAULT_SCOUT_STREAM_URL,
  ROSBRIDGE_URL,
  type VehicleId,
} from "../api/client";
import { useDashboardStore } from "../state/store";

const CAMERA_TOPIC = "/oak/rgb/image_raw/compressed";
const CAMERA_TOPIC_TYPE = "sensor_msgs/msg/CompressedImage";
const RECONNECT_DELAY_MS = 2000;

type CameraFeedStatus = "connecting" | "live" | "reconnecting" | "error";

interface RosBridgeImageMessage {
  op?: unknown;
  topic?: unknown;
  msg?: {
    format?: unknown;
    data?: unknown;
  };
}

function isRosBridgeImageMessage(value: unknown): value is RosBridgeImageMessage {
  return typeof value === "object" && value !== null;
}

export function CameraFeed(): React.JSX.Element {
  const { state } = useDashboardStore();
  const selectedCamera: VehicleId = "SCOUT01";
  const [streamUrl, setStreamUrl] = useState<string>(DEFAULT_SCOUT_STREAM_URL);
  const [urlInput, setUrlInput] = useState<string>(DEFAULT_SCOUT_STREAM_URL);
  const [streamError, setStreamError] = useState<boolean>(false);
  const [rosFrame, setRosFrame] = useState<string>("");
  const [rosStatus, setRosStatus] = useState<CameraFeedStatus>("connecting");
  const [isExpanded, setIsExpanded] = useState<boolean>(false);

  const activeUrl = streamUrl.trim();
  const hasError = streamError;
  const vehicleState = state.vehicles.SCOUT01;

  useEffect(() => {
    if (activeUrl) return;

    setRosFrame("");
    let active = true;
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let staleTimer: ReturnType<typeof setTimeout> | undefined;
    let hasConnected = false;

    const connect = (): void => {
      if (!active) return;
      setRosStatus(hasConnected ? "reconnecting" : "connecting");

      try {
        socket = new WebSocket(ROSBRIDGE_URL);
      } catch {
        setRosStatus("error");
        reconnectTimer = setTimeout(connect, RECONNECT_DELAY_MS);
        return;
      }

      socket.onopen = () => {
        if (!active || !socket) return;
        hasConnected = true;
        setRosStatus("connecting");
        socket.send(
          JSON.stringify({
            op: "subscribe",
            id: "operation-shadow-scout-camera",
            topic: CAMERA_TOPIC,
            type: CAMERA_TOPIC_TYPE,
            throttle_rate: 200,
            queue_length: 1,
          }),
        );
      };

      socket.onmessage = (event: MessageEvent<string>) => {
        if (!active) return;
        try {
          const message: unknown = JSON.parse(event.data);
          if (!isRosBridgeImageMessage(message)) return;
          if (message.op !== "publish" || message.topic !== CAMERA_TOPIC) return;

          const image = message.msg;
          if (
            typeof image?.data !== "string" ||
            typeof image.format !== "string"
          ) {
            setRosStatus("error");
            return;
          }

          const format = image.format.toLowerCase();
          const mimeType = format.includes("jpeg") || format.includes("jpg")
            ? "image/jpeg"
            : format.includes("png")
              ? "image/png"
              : null;
          if (!mimeType) {
            setRosStatus("error");
            return;
          }

          setRosFrame(`data:${mimeType};base64,${image.data}`);
          setRosStatus("live");
          if (staleTimer) clearTimeout(staleTimer);
          staleTimer = setTimeout(() => {
            if (active) setRosStatus("reconnecting");
          }, 5000);
        } catch {
          setRosStatus("error");
        }
      };

      socket.onerror = () => {
        if (active) setRosStatus("error");
      };

      socket.onclose = () => {
        if (!active) return;
        if (staleTimer) clearTimeout(staleTimer);
        setRosStatus("reconnecting");
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
  }, [activeUrl]);

  const handleApplyStreamUrl = (
    event: React.FormEvent<HTMLFormElement>,
  ): void => {
    event.preventDefault();
    const cleaned = urlInput.trim();
    setStreamUrl(cleaned);
    setStreamError(false);
  };

  return (
    <section
      className={`panel${isExpanded ? " camera-panel-expanded" : ""}`}
      aria-label="Camera Feed"
    >
      <div className="panel-header">
        <h2 className="panel-title">
          OPTICAL FEED // {selectedCamera}
        </h2>
        <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          <button
            type="button"
            className="btn btn-secondary"
            style={{ padding: "2px 7px", fontSize: "9px" }}
            onClick={() => setIsExpanded((prev) => !prev)}
            aria-label={isExpanded ? "Dock camera feed" : "Expand camera feed"}
          >
            {isExpanded ? "DOCK VIEW" : "EXPAND"}
          </button>
          <span className="badge">
            {vehicleState.mode === "simulated" ? "SIMULATED" : "REAL"}
          </span>
        </div>
      </div>

      <div className="panel-body">
        <div
          className="camera-viewport"
          data-testid="camera-viewport"
        >
          <div className="reticle-corners" />

          {/* Optical Stream Status Overlay */}
          <div className="camera-overlay">
            <div className="camera-overlay-top">
              <span style={{ color: "var(--marker-scout)", fontWeight: 500 }}>
                SCOUT01 OPTICS // READ-ONLY STREAM
              </span>
              <span
                className={`badge ${activeUrl || rosStatus === "live" ? "badge-nominal" : "badge-warning"}`}
                style={{ fontSize: "9px", padding: "1px 5px" }}
              >
                {activeUrl
                  ? "DIRECT MJPEG"
                  : rosStatus === "live"
                    ? "ROS LIVE"
                    : rosStatus === "reconnecting"
                      ? "ROS RETRY"
                      : "ROS CONNECTING"}
              </span>
            </div>
            <div className="camera-overlay-bottom">
              <span style={{ color: "var(--text-secondary)" }}>{selectedCamera} // FORWARD OPTICS</span>
              <span style={{ color: "var(--text-secondary)" }}>
                X: {vehicleState.position.x.toFixed(2)}m Y: {vehicleState.position.y.toFixed(2)}m
              </span>
            </div>
          </div>

          {!activeUrl && !rosFrame ? (
            <div role="status" style={{ padding: "12px", textAlign: "center", zIndex: 1 }}>
              <div
                style={{
                  color: rosStatus === "error" ? "var(--status-critical-bright)" : "var(--status-warning)",
                  fontFamily: "var(--font-data)",
                  fontWeight: 600,
                  fontSize: "11px",
                }}
              >
                {rosStatus === "error"
                  ? "ROS CAMERA FEED ERROR // CHECK ROSBRIDGE CONNECTION AND IMAGE FORMAT"
                  : rosStatus === "reconnecting"
                    ? "ROS CAMERA FEED RECONNECTING"
                    : "CONNECTING TO SCOUT01 ROS CAMERA"}
              </div>
              <div
                style={{
                  color: "var(--text-muted)",
                  fontSize: "10px",
                  marginTop: "4px",
                  lineHeight: 1.4,
                }}
              >
                {rosStatus === "connecting"
                  ? `Waiting for ${CAMERA_TOPIC} over ${ROSBRIDGE_URL}`
                  : "Camera frames connect directly from SCOUT01 to this browser; video is not relayed by FastAPI."}
              </div>
            </div>
          ) : activeUrl && hasError ? (
            <div role="alert" style={{ padding: "12px", textAlign: "center", zIndex: 1 }}>
              <div
                style={{
                  color: "var(--status-critical-bright)",
                  fontFamily: "var(--font-data)",
                  fontWeight: 600,
                  fontSize: "11px",
                }}
              >
                CAMERA STREAM ERROR // DIRECT MJPEG FEED UNREACHABLE
              </div>
              <div
                style={{
                  color: "var(--text-muted)",
                  fontFamily: "var(--font-data)",
                  fontSize: "10px",
                  marginTop: "4px",
                }}
              >
                Failed to load stream from {activeUrl}
              </div>
            </div>
          ) : activeUrl ? (
            <img
              src={activeUrl}
              alt={`${selectedCamera} direct MJPEG camera stream`}
              style={{
                width: "100%",
                height: "100%",
                objectFit: "contain",
                display: "block",
              }}
              onError={() => setStreamError(true)}
            />
          ) : (
            <img
              src={rosFrame}
              alt={`${selectedCamera} direct ROS camera stream`}
              style={{
                width: "100%",
                height: "100%",
                objectFit: "contain",
                display: "block",
              }}
              onError={() => setStreamError(true)}
            />
          )}
        </div>

        <form
          onSubmit={handleApplyStreamUrl}
          style={{ display: "flex", gap: "4px" }}
        >
          <input
            type="url"
            className="input"
            aria-label={`${selectedCamera} MJPEG Stream URL`}
            placeholder="Optional direct MJPEG URL"
            value={urlInput}
            onChange={(e) => setUrlInput(e.target.value)}
            style={{ fontSize: "11px", padding: "4px 8px" }}
          />
          <button
            type="submit"
            className="btn btn-secondary"
            style={{ padding: "4px 10px", fontSize: "10px", whiteSpace: "nowrap" }}
          >
            USE MJPEG URL
          </button>
        </form>
      </div>
    </section>
  );
}
