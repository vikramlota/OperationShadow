import React, { useCallback, useEffect, useRef, useState } from "react";
import { WS_SCOUT_CONTROL_URL } from "../api/client";

type MovementCommand = "W" | "A" | "S" | "D";
type ControlCommand = MovementCommand | "X";
type LinkState = "CONNECTING" | "CONNECTED" | "DISCONNECTED" | "ERROR";

const KEY_COMMANDS: Record<string, MovementCommand> = {
  w: "W",
  a: "A",
  s: "S",
  d: "D",
};
const configuredRateHz = Number(import.meta.env.VITE_SCOUT_CONTROL_RATE_HZ ?? 10);
const KEEPALIVE_RATE_HZ = Number.isFinite(configuredRateHz)
  ? Math.min(20, Math.max(5, configuredRateHz))
  : 10;
const KEEPALIVE_INTERVAL_MS = Math.round(1000 / KEEPALIVE_RATE_HZ);
const CONTROL_KEYS = new Set(["w", "a", "s", "d", "x"]);

interface ControlStatusFrame {
  type?: unknown;
  mode?: unknown;
  transmitter_connected?: unknown;
  physical_control_available?: unknown;
  control_client_connected?: unknown;
  current_command?: unknown;
  message?: unknown;
}

function isEditableTarget(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    (target.isContentEditable ||
      target.closest("input, textarea, select, [contenteditable='true']") !== null)
  );
}

export function ScoutKeyboardControl(): React.JSX.Element {
  const socketRef = useRef<WebSocket | null>(null);
  const heldCommandRef = useRef<MovementCommand | null>(null);
  const keepaliveRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [linkState, setLinkState] = useState<LinkState>("CONNECTING");
  const [currentCommand, setCurrentCommand] = useState<ControlCommand>("X");
  const [transmitterConnected, setTransmitterConnected] = useState(false);
  const [serialMode, setSerialMode] = useState(false);
  const [physicalControlAvailable, setPhysicalControlAvailable] = useState(false);
  const [controlError, setControlError] = useState<string | null>(null);

  const sendCommand = useCallback((command: ControlCommand): void => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setCurrentCommand("X");
      heldCommandRef.current = null;
      return;
    }
    socket.send(JSON.stringify({ type: "command", command }));
    setCurrentCommand(command);
    heldCommandRef.current = command === "X" ? null : command;
  }, []);

  const releaseMovement = useCallback((): void => {
    if (keepaliveRef.current !== null) {
      clearInterval(keepaliveRef.current);
      keepaliveRef.current = null;
    }
    heldCommandRef.current = null;
    sendCommand("X");
  }, [sendCommand]);

  const beginMovement = useCallback(
    (command: MovementCommand): void => {
      const held = heldCommandRef.current;
      if (held && held !== command) {
        releaseMovement();
        setControlError("Only one movement direction may be held at a time; control stopped.");
        return;
      }
      if (held === command) return;
      setControlError(null);
      sendCommand(command);
      keepaliveRef.current = setInterval(() => {
        const socket = socketRef.current;
        if (socket?.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: "keepalive" }));
        }
      }, KEEPALIVE_INTERVAL_MS);
    },
    [releaseMovement, sendCommand],
  );

  useEffect(() => {
    let active = true;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let connectTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = (): void => {
      if (!active) return;
      setLinkState("CONNECTING");
      let socket: WebSocket;
      try {
        socket = new WebSocket(WS_SCOUT_CONTROL_URL);
      } catch {
        setLinkState("ERROR");
        retryTimer = setTimeout(connect, 1500);
        return;
      }
      socketRef.current = socket;

      socket.onopen = () => {
        if (!active) return;
        heldCommandRef.current = null;
        setCurrentCommand("X");
        setTransmitterConnected(false);
        setSerialMode(false);
        setPhysicalControlAvailable(false);
        setControlError(null);
        setLinkState("CONNECTED");
      };

      socket.onmessage = (event: MessageEvent<string>) => {
        if (!active) return;
        try {
          const frame: unknown = JSON.parse(event.data);
          if (typeof frame !== "object" || frame === null) return;
          const status = frame as ControlStatusFrame;
          if (status.type === "status") {
            setTransmitterConnected(status.transmitter_connected === true);
            setSerialMode(status.mode === "serial");
            setPhysicalControlAvailable(status.physical_control_available === true);
            if (
              status.current_command === "W" ||
              status.current_command === "A" ||
              status.current_command === "S" ||
              status.current_command === "D" ||
              status.current_command === "X"
            ) {
              setCurrentCommand(status.current_command);
            }
          } else if (status.type === "error") {
            setControlError(
              typeof status.message === "string"
                ? status.message
                : "Control server rejected a message.",
            );
          }
        } catch {
          setControlError("Control server sent an invalid status frame.");
        }
      };

      socket.onerror = () => {
        if (active) {
          setLinkState("ERROR");
          setCurrentCommand("X");
          heldCommandRef.current = null;
        }
      };

      socket.onclose = () => {
        if (!active) return;
        setLinkState("DISCONNECTED");
        setCurrentCommand("X");
        setTransmitterConnected(false);
        heldCommandRef.current = null;
        if (keepaliveRef.current !== null) {
          clearInterval(keepaliveRef.current);
          keepaliveRef.current = null;
        }
        retryTimer = setTimeout(connect, 1500);
      };
    };

    connectTimer = setTimeout(connect, 100);
    return () => {
      active = false;
      if (retryTimer !== null) clearTimeout(retryTimer);
      if (connectTimer !== null) clearTimeout(connectTimer);
      if (keepaliveRef.current !== null) {
        clearInterval(keepaliveRef.current);
        keepaliveRef.current = null;
      }
      heldCommandRef.current = null;
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent): void => {
      const key = event.key.toLowerCase();
      if (!CONTROL_KEYS.has(key) || isEditableTarget(event.target)) return;
      if (linkState !== "CONNECTED") return;
      event.preventDefault();
      if (event.repeat) return;
      if (key === "x") {
        releaseMovement();
        return;
      }
      beginMovement(KEY_COMMANDS[key]);
    };

    const onKeyUp = (event: KeyboardEvent): void => {
      const key = event.key.toLowerCase();
      if (!CONTROL_KEYS.has(key) || isEditableTarget(event.target)) return;
      event.preventDefault();
      if (key === "x") return;
      if (heldCommandRef.current === KEY_COMMANDS[key]) releaseMovement();
    };

    const stopOnFocusLoss = (): void => releaseMovement();
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", stopOnFocusLoss);
    document.addEventListener("visibilitychange", stopOnFocusLoss);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", stopOnFocusLoss);
      document.removeEventListener("visibilitychange", stopOnFocusLoss);
    };
  }, [beginMovement, linkState, releaseMovement]);

  const linkLabel =
    linkState === "CONNECTED"
      ? serialMode
        ? transmitterConnected
          ? "SERIAL LINKED"
          : "SERIAL OFFLINE"
        : "SIMULATOR LINKED"
      : linkState === "CONNECTING"
        ? "CONNECTING"
        : linkState;

  return (
    <section className="panel" aria-label="SCOUT01 Keyboard Control">
      <div className="panel-header">
        <h2 className="panel-title">SCOUT01 MANUAL INPUT</h2>
        <span className={`badge ${linkState === "CONNECTED" ? "badge-nominal" : "badge-warning"}`}>
          {linkLabel}
        </span>
      </div>
      <div className="panel-body scout-control-panel">
        <div className="scout-control-safety" role="note">
          {serialMode
            ? physicalControlAvailable
              ? "LIVE SERIAL CONTROL // USB LOSS STOP REQUIRES TRANSMITTER FAILSAFE"
              : "SERIAL CONTROL ARMED // TRANSMITTER OFFLINE // MOVEMENT DISABLED"
            : "SIMULATION ONLY // ESP32 TRANSMITTER OFFLINE // NO MOTOR OUTPUT"}
        </div>
        <div className="scout-control-status">
          <span>CURRENT: {currentCommand}</span>
          <span>TRANSMITTER: {transmitterConnected ? "CONNECTED" : "NOT CONNECTED"}</span>
        </div>
        <div
          className="scout-control-keypad"
          aria-label={serialMode ? "SCOUT01 movement controls" : "Simulated movement controls"}
        >
          <span />
          {(["W"] as const).map((command) => (
            <button
              key={command}
              type="button"
              className="btn btn-secondary"
              disabled={
                linkState !== "CONNECTED" || (serialMode && !physicalControlAvailable)
              }
              onPointerDown={(event) => {
                event.preventDefault();
                beginMovement(command);
              }}
              onPointerUp={releaseMovement}
              onPointerCancel={releaseMovement}
              onPointerLeave={(event) => {
                if (event.buttons !== 0) releaseMovement();
              }}
              aria-label={`${serialMode ? "Send" : "Simulate"} ${command} forward`}
            >
              W
            </button>
          ))}
          <span />
          {(["A", "S", "D"] as const).map((command) => (
            <button
              key={command}
              type="button"
              className="btn btn-secondary"
              disabled={
                linkState !== "CONNECTED" || (serialMode && !physicalControlAvailable)
              }
              onPointerDown={(event) => {
                event.preventDefault();
                beginMovement(command);
              }}
              onPointerUp={releaseMovement}
              onPointerCancel={releaseMovement}
              onPointerLeave={(event) => {
                if (event.buttons !== 0) releaseMovement();
              }}
              aria-label={`${serialMode ? "Send" : "Simulate"} ${command}`}
            >
              {command}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="btn btn-secondary scout-control-stop"
          disabled={linkState !== "CONNECTED"}
          onClick={releaseMovement}
          aria-label={serialMode ? "Send X stop" : "Send simulated X stop"}
        >
          X // SIM STOP
        </button>
        {controlError && (
          <div className="c2-alert-box c2-alert-warn" role="alert">
            {controlError}
          </div>
        )}
        <span className="panel-meta">
          HOLD W/A/S/D // RELEASE OR X TO STOP // {KEEPALIVE_RATE_HZ} HZ SIM LEASE
        </span>
      </div>
    </section>
  );
}
