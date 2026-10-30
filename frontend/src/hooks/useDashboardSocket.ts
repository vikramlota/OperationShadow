import { useEffect, useRef } from "react";
import {
  WS_DASHBOARD_URL,
  type EventReadPayload,
  type ReasoningTraceReadPayload,
} from "../api/client";
import {
  useDashboardStore,
  type DashboardSnapshotWirePayload,
  type DashboardSocketEnvelope,
  type MissionStatusPushPayload,
  type TelemetryPushPayload,
} from "../state/store";

export interface UseDashboardSocketOptions {
  enabled?: boolean;
  wsUrl?: string;
  reconnectDelayMs?: number;
}

/**
 * Single managed WebSocket connection hook (Architecture §5.2, §8.1, §8.4).
 * Connects to `/ws/dashboard`, hydrates full state from the initial `snapshot`
 * envelope on connect and reconnect, dispatches live pushes (`telemetry`,
 * `event`, `reasoning_trace`, `mission_status`), and cleans up on unmount.
 */
export function useDashboardSocket(
  options: UseDashboardSocketOptions = {},
): { connected: boolean; reconnectAttempt: number; lastError: string | null } {
  const {
    enabled = true,
    wsUrl = WS_DASHBOARD_URL,
    reconnectDelayMs = 1500,
  } = options;
  const { state, dispatch } = useDashboardStore();
  const attemptRef = useRef<number>(0);

  useEffect(() => {
    if (!enabled || typeof WebSocket === "undefined") {
      return;
    }

    let isUnmounted = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = (): void => {
      if (isUnmounted) {
        return;
      }

      try {
        socket = new WebSocket(wsUrl);
      } catch (err) {
        attemptRef.current += 1;
        dispatch({
          type: "SET_WS_RECONNECTING",
          payload: {
            attempt: attemptRef.current,
            error: err instanceof Error ? err.message : "WebSocket init error",
          },
        });
        reconnectTimer = setTimeout(connect, reconnectDelayMs);
        return;
      }

      socket.onopen = () => {
        if (isUnmounted) {
          return;
        }
        attemptRef.current = 0;
        dispatch({ type: "SET_WS_CONNECTED", payload: true });
      };

      socket.onmessage = (event: MessageEvent<string>) => {
        if (isUnmounted) {
          return;
        }
        try {
          const envelope = JSON.parse(
            event.data,
          ) as DashboardSocketEnvelope<unknown>;
          switch (envelope.type) {
            case "snapshot":
              dispatch({
                type: "APPLY_WS_SNAPSHOT",
                payload: envelope.payload as DashboardSnapshotWirePayload,
              });
              break;
            case "telemetry":
              dispatch({
                type: "APPLY_WS_TELEMETRY",
                payload: {
                  telemetry: envelope.payload as TelemetryPushPayload,
                  sequence: envelope.sequence,
                },
              });
              break;
            case "event":
              dispatch({
                type: "APPLY_WS_EVENT",
                payload: envelope.payload as EventReadPayload,
              });
              break;
            case "reasoning_trace":
              dispatch({
                type: "APPLY_WS_REASONING_TRACE",
                payload: envelope.payload as ReasoningTraceReadPayload,
              });
              break;
            case "mission_status":
              dispatch({
                type: "APPLY_WS_MISSION_STATUS",
                payload: envelope.payload as MissionStatusPushPayload,
              });
              break;
            default:
              break;
          }
        } catch {
          // Ignore malformed non-JSON frames safely
        }
      };

      socket.onerror = () => {
        if (isUnmounted) {
          return;
        }
        dispatch({
          type: "SET_WS_RECONNECTING",
          payload: {
            attempt: attemptRef.current + 1,
            error: "Dashboard WebSocket transport error",
          },
        });
      };

      socket.onclose = () => {
        if (isUnmounted) {
          return;
        }
        attemptRef.current += 1;
        dispatch({
          type: "SET_WS_RECONNECTING",
          payload: {
            attempt: attemptRef.current,
            error: "Dashboard WebSocket disconnected — awaiting resync",
          },
        });
        reconnectTimer = setTimeout(connect, reconnectDelayMs);
      };
    };

    connect();

    return () => {
      isUnmounted = true;
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
      }
      if (socket !== null) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        try {
          socket.close();
        } catch {
          // Ignore close errors during unmount
        }
      }
    };
  }, [enabled, wsUrl, reconnectDelayMs, dispatch]);

  return {
    connected: state.wsConnected,
    reconnectAttempt: state.wsReconnectAttempt,
    lastError: state.wsLastError,
  };
}
