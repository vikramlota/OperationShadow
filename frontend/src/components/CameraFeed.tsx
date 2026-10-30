import React, { useState } from "react";
import {
  DEFAULT_SCOUT_STREAM_URL,
  type VehicleId,
} from "../api/client";
import { useDashboardStore } from "../state/store";

export function CameraFeed(): React.JSX.Element {
  const { state } = useDashboardStore();
  const selectedCamera: VehicleId = "SCOUT01";
  const [streamUrl, setStreamUrl] = useState<string>(DEFAULT_SCOUT_STREAM_URL);
  const [urlInput, setUrlInput] = useState<string>(DEFAULT_SCOUT_STREAM_URL);
  const [streamError, setStreamError] = useState<boolean>(false);

  const activeUrl = streamUrl.trim();
  const hasError = streamError;
  const vehicleState = state.vehicles.SCOUT01;

  const handleApplyStreamUrl = (
    event: React.FormEvent<HTMLFormElement>,
  ): void => {
    event.preventDefault();
    const cleaned = urlInput.trim();
    setStreamUrl(cleaned);
    setStreamError(false);
  };

  return (
    <section className="panel" aria-label="Camera Feed">
      <div className="panel-header">
        <h2 className="panel-title">
          OPTICAL FEED // {selectedCamera}
        </h2>
        <span className="badge">
          {vehicleState.mode === "simulated" ? "SIMULATED" : "REAL"}
        </span>
      </div>

      <div className="panel-body">
        <div
          className="camera-viewport"
          data-testid="camera-viewport"
        >
          <div className="reticle-corners" />

          {/* Tactical Optical HUD Overlay */}
          <div className="camera-overlay">
            <div className="camera-overlay-top">
              <span style={{ color: "var(--marker-scout)", fontWeight: 500 }}>
                HDG: 042° NE // 30 FPS
              </span>
              <span className="badge badge-nominal" style={{ fontSize: "9px", padding: "1px 5px" }}>
                DIRECT MJPEG
              </span>
            </div>
            <div className="camera-overlay-bottom">
              <span style={{ color: "var(--text-secondary)" }}>{selectedCamera} // FORWARD OPTICS</span>
              <span style={{ color: "var(--text-secondary)" }}>
                X: {vehicleState.position.x.toFixed(2)}m Y: {vehicleState.position.y.toFixed(2)}m
              </span>
            </div>
          </div>

          {!activeUrl ? (
            <div role="status" style={{ padding: "12px", textAlign: "center", zIndex: 1 }}>
              <div
                style={{
                  color: "var(--status-warning)",
                  fontFamily: "var(--font-data)",
                  fontWeight: 600,
                  fontSize: "11px",
                }}
              >
                CAMERA STREAM UNAVAILABLE // NO DIRECT MJPEG URL CONFIGURED
              </div>
              <div
                style={{
                  color: "var(--text-muted)",
                  fontSize: "10px",
                  marginTop: "4px",
                  lineHeight: 1.4,
                }}
              >
                Direct vehicle stream bypasses backend (AGENTS.md §8.3).
              </div>
            </div>
          ) : hasError ? (
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
          ) : (
            <img
              src={activeUrl}
              alt={`${selectedCamera} direct MJPEG camera stream`}
              style={{
                width: "100%",
                height: "100%",
                objectFit: "cover",
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
            placeholder="Stream URL (http://...)"
            value={urlInput}
            onChange={(e) => setUrlInput(e.target.value)}
            style={{ fontSize: "11px", padding: "4px 8px" }}
          />
          <button
            type="submit"
            className="btn btn-secondary"
            style={{ padding: "4px 10px", fontSize: "10px", whiteSpace: "nowrap" }}
          >
            SET URL
          </button>
        </form>
      </div>
    </section>
  );
}
