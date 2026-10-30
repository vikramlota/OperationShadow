import React, { useState } from "react";
import {
  DEFAULT_HAWK_STREAM_URL,
  DEFAULT_SCOUT_STREAM_URL,
  type VehicleId,
} from "../api/client";
import { useDashboardStore } from "../state/store";

export function CameraFeed(): React.JSX.Element {
  const { state } = useDashboardStore();
  const [selectedCamera, setSelectedCamera] = useState<VehicleId>("SCOUT01");
  const [streamUrls, setStreamUrls] = useState<Record<VehicleId, string>>({
    SCOUT01: DEFAULT_SCOUT_STREAM_URL,
    HAWK01: DEFAULT_HAWK_STREAM_URL,
  });
  const [urlInput, setUrlInput] = useState<string>(DEFAULT_SCOUT_STREAM_URL);
  const [streamError, setStreamError] = useState<Record<VehicleId, boolean>>({
    SCOUT01: false,
    HAWK01: false,
  });

  const activeUrl = streamUrls[selectedCamera].trim();
  const hasError = streamError[selectedCamera];
  const vehicleState = state.vehicles[selectedCamera];

  const handleSelectCamera = (vid: VehicleId): void => {
    setSelectedCamera(vid);
    setUrlInput(streamUrls[vid]);
  };

  const handleApplyStreamUrl = (
    event: React.FormEvent<HTMLFormElement>,
  ): void => {
    event.preventDefault();
    const cleaned = urlInput.trim();
    setStreamUrls((prev) => ({ ...prev, [selectedCamera]: cleaned }));
    setStreamError((prev) => ({ ...prev, [selectedCamera]: false }));
  };

  return (
    <section className="c2-panel" aria-label="Camera Feed">
      <div className="c2-panel-header">
        <h2 className="c2-panel-title">
          DIRECT MJPEG OPTICAL FEED // {selectedCamera}
        </h2>
        <span className="c2-badge">
          {vehicleState.mode === "simulated" ? "SIMULATED VEHICLE" : "REAL VEHICLE"}
        </span>
      </div>

      <div className="c2-panel-body">
        <div style={{ display: "flex", gap: "6px" }}>
          {(["SCOUT01", "HAWK01"] as const).map((vid, idx) => {
            const active = selectedCamera === vid;
            return (
              <button
                key={vid}
                type="button"
                className={`c2-btn ${active ? "c2-btn-active" : "c2-btn-secondary"}`}
                onClick={() => handleSelectCamera(vid)}
                aria-pressed={active}
                aria-label={`Select ${vid} Camera Feed`}
              >
                {vid} [CAM-0{idx + 1}]
              </button>
            );
          })}
        </div>

        <form
          onSubmit={handleApplyStreamUrl}
          style={{ display: "flex", gap: "6px" }}
        >
          <input
            type="url"
            className="c2-input"
            aria-label={`${selectedCamera} MJPEG Stream URL`}
            placeholder="Configure direct vehicle MJPEG stream URL (http://...)"
            value={urlInput}
            onChange={(e) => setUrlInput(e.target.value)}
          />
          <button type="submit" className="c2-btn c2-btn-secondary">
            SET URL
          </button>
        </form>

        <div
          style={{
            minHeight: "150px",
            backgroundColor: "var(--bg-recessed)",
            border: "1px solid var(--border-subtle)",
            borderRadius: "2px",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            padding: "12px",
            textAlign: "center",
          }}
          data-testid="camera-viewport"
        >
          {!activeUrl ? (
            <div role="status">
              <div
                className="c2-mono"
                style={{
                  color: "var(--status-warning)",
                  fontWeight: 700,
                  fontSize: "12px",
                }}
              >
                CAMERA STREAM UNAVAILABLE // NO DIRECT MJPEG URL CONFIGURED
              </div>
              <div
                style={{
                  color: "var(--text-secondary)",
                  fontSize: "11px",
                  marginTop: "4px",
                }}
              >
                {selectedCamera} is operating in {vehicleState.mode.toUpperCase()}{" "}
                mode ({vehicleState.connection}). Camera feeds bypass the FastAPI
                backend and require a direct companion MJPEG URL.
              </div>
            </div>
          ) : hasError ? (
            <div role="alert">
              <div
                className="c2-mono"
                style={{
                  color: "var(--status-critical-bright)",
                  fontWeight: 700,
                  fontSize: "12px",
                }}
              >
                CAMERA STREAM ERROR // DIRECT MJPEG FEED UNREACHABLE
              </div>
              <div
                className="c2-mono"
                style={{
                  color: "var(--text-secondary)",
                  fontSize: "11px",
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
                maxWidth: "100%",
                maxHeight: "180px",
                display: "block",
                border: "1px solid var(--border-subtle)",
              }}
              onError={() =>
                setStreamError((prev) => ({ ...prev, [selectedCamera]: true }))
              }
            />
          )}
        </div>
      </div>
    </section>
  );
}
