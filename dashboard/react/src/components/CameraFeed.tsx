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
    <section className="c2-panel" aria-label="Camera Feed">
      <div className="c2-panel-header">
        <h2 className="c2-panel-title">
          DIRECT MJPEG OPTICAL FEED // {selectedCamera}
        </h2>
        <span className="c2-badge">
          {vehicleState?.mode === "simulated" ? "SIMULATED VEHICLE" : "REAL VEHICLE"}
        </span>
      </div>

      <div className="c2-panel-body">
        <div style={{ display: "flex", gap: "6px" }}>
          <button
            type="button"
            className="c2-btn c2-btn-active"
            aria-pressed={true}
            aria-label="Select SCOUT01 Camera Feed"
          >
            SCOUT01 [CAM-01]
          </button>
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
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "8px",
                alignItems: "center",
              }}
            >
              <span
                style={{
                  color: "var(--amber-warning)",
                  fontFamily: "var(--font-mono)",
                  fontWeight: 600,
                  fontSize: "12px",
                  letterSpacing: "0.5px",
                }}
              >
                CAMERA STREAM UNAVAILABLE // NO DIRECT MJPEG URL CONFIGURED
              </span>
              <p
                style={{
                  fontSize: "11px",
                  color: "var(--text-muted)",
                  margin: 0,
                  maxWidth: "320px",
                  lineHeight: "1.4",
                }}
              >
                No direct HTTP MJPEG camera stream configured for {selectedCamera}. Per
                Architecture §8.3, video is never relayed through the backend;
                enter vehicle IP &amp; port above to view live optical stream.
              </p>
            </div>
          ) : hasError ? (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "8px",
                alignItems: "center",
              }}
            >
              <span
                style={{
                  color: "var(--crimson-alert)",
                  fontFamily: "var(--font-mono)",
                  fontWeight: 600,
                  fontSize: "12px",
                }}
              >
                CAMERA STREAM ERROR // DIRECT MJPEG FEED UNREACHABLE
              </span>
              <p
                style={{
                  fontSize: "11px",
                  color: "var(--text-muted)",
                  margin: 0,
                }}
              >
                Failed to load stream from {activeUrl}
              </p>
            </div>
          ) : (
            <img
              src={activeUrl}
              alt={`${selectedCamera} direct MJPEG camera stream`}
              onError={() => setStreamError(true)}
              style={{
                width: "100%",
                maxHeight: "240px",
                objectFit: "contain",
                display: "block",
              }}
            />
          )}
        </div>
      </div>
    </section>
  );
}
