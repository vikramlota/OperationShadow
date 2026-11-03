#!/usr/bin/env python3
"""Read-only backend signal probe (`scripts/signal_probe.py` — Workstream E).

Polls the Operation Shadow backend read-only REST endpoints:
  - `GET /health`
  - `GET /vehicle/SCOUT01/status`
  - `GET /vehicle/SCOUT01/sensors`
  - `GET /vehicle/SCOUT01/detections`
for `N` seconds (`--duration`, default 5.0s; `--interval`, default 0.5s) and prints a
per-signal summary table showing:
  - Present / Absent
  - Last Value
  - Observed Update Rate (Hz)
  - Staleness
  - REAL / SIMULATED source flag

Safety Invariant:
  - Strictly READ-ONLY. Never sends any POST/PUT/DELETE request or WebSocket command
    to any vehicle or actuator endpoint.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import sys
import time
from typing import Any
import urllib.request


@dataclass
class SignalStats:
    name: str
    present: bool = False
    last_value: Any = None
    sample_count: int = 0
    change_count: int = 0
    last_stamp_or_seq: Any = None
    stale: str = "N/A"
    mode_flag: str = "SIMULATED"
    notes: str = ""
    _seen_first_at: float | None = field(default=None, repr=False)
    _seen_last_at: float | None = field(default=None, repr=False)

    def record(
        self,
        *,
        present: bool,
        value: Any,
        stamp_or_seq: Any,
        stale: str,
        mode_flag: str,
        now: float,
    ) -> None:
        self.sample_count += 1
        self.stale = stale
        self.mode_flag = mode_flag
        if not present:
            return
        if not self.present:
            self.present = True
            self._seen_first_at = now
            self._seen_last_at = now
            self.last_value = value
            self.last_stamp_or_seq = stamp_or_seq
            self.change_count = 1
            return

        if stamp_or_seq is not None and stamp_or_seq != self.last_stamp_or_seq:
            self.change_count += 1
            self.last_stamp_or_seq = stamp_or_seq
            self._seen_last_at = now
        elif stamp_or_seq is None and value != self.last_value:
            self.change_count += 1
            self._seen_last_at = now

        self.last_value = value

    def update_rate_hz(self, total_duration_s: float) -> float:
        if not self.present or self.change_count <= 0 or total_duration_s <= 0.0:
            return 0.0
        if self.change_count == 1:
            return round(1.0 / total_duration_s, 2)
        return round((self.change_count - 1) / total_duration_s, 2)


def _fetch_json_http(base_url: str, path: str, timeout: float = 2.0) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))  # type: ignore[no-any-return]


def run_probe(
    *,
    base_url: str = "http://127.0.0.1:8000",
    duration_sec: float = 5.0,
    interval_sec: float = 0.5,
    in_process: bool = False,
) -> dict[str, SignalStats]:
    signals: dict[str, SignalStats] = {
        "camera_detections": SignalStats("camera_detections (/detections_json)"),
        "battery": SignalStats("battery (%)"),
        "connection_state": SignalStats("connection_state"),
        "tof_ultrasonic_distance": SignalStats("tof_ultrasonic_distance (front_distance_cm)"),
        "temperature": SignalStats("temperature (C)"),
        "gas": SignalStats("gas (gas_level / gas_raw)"),
        "pose": SignalStats("pose (position_x, position_y, alt)"),
        "lidar": SignalStats("lidar (sensor_health.lidar)"),
    }

    client_ctx = None
    test_client = None
    if in_process:
        repo_root = Path(__file__).resolve().parent.parent
        for p in (
            str(repo_root / "backend" / "fastapi"),
            str(repo_root / "backend"),
            str(repo_root),
        ):
            if p not in sys.path:
                sys.path.insert(0, p)
        from fastapi.testclient import TestClient
        from main import create_app

        app = create_app()
        client_ctx = TestClient(app)
        test_client = client_ctx.__enter__()

    def _get(path: str) -> dict[str, Any]:
        if test_client is not None:
            resp = test_client.get(path)
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
        return _fetch_json_http(base_url, path)

    t_start = time.monotonic()
    deadline = t_start + max(0.1, duration_sec)
    try:
        while True:
            now = time.monotonic()
            status_data = _get("/vehicle/SCOUT01/status")
            sensors_data = _get("/vehicle/SCOUT01/sensors")
            det_data = _get("/vehicle/SCOUT01/detections")

            veh_mode = str(status_data.get("mode", "simulated")).upper()
            veh_flag = "REAL" if veh_mode == "REAL" else "SIMULATED"
            veh_stale = "STALE" if status_data.get("is_stale") else "FRESH"
            hb_stamp = status_data.get("last_heartbeat")
            raw_sensors = sensors_data.get("raw_sensors") or {}

            # 1. Camera detections
            has_det_frame = bool(det_data.get("has_received_frame"))
            det_objects = det_data.get("objects") or []
            det_source = str(det_data.get("telemetry_source", "mock")).lower()
            det_flag = (
                "REAL"
                if (det_source == "rosbridge" and det_data.get("ros_connection_state") == "CONNECTED")
                else "SIMULATED"
            )
            det_stale = (
                "STALE"
                if det_data.get("is_stale")
                else ("FRESH" if has_det_frame else f"NO_FRAME ({det_data.get('ros_connection_state')})")
            )
            signals["camera_detections"].record(
                present=has_det_frame,
                value=f"{len(det_objects)} obj(s) in {det_data.get('frame')}" if has_det_frame else None,
                stamp_or_seq=det_data.get("stamp"),
                stale=det_stale,
                mode_flag=det_flag,
                now=now,
            )

            # 2. Battery
            bat_val = status_data.get("battery")
            signals["battery"].record(
                present=bat_val is not None,
                value=bat_val,
                stamp_or_seq=hb_stamp or sensors_data.get("sequence"),
                stale=veh_stale,
                mode_flag=veh_flag,
                now=now,
            )

            # 3. Connection state
            conn_val = status_data.get("connection")
            signals["connection_state"].record(
                present=conn_val is not None,
                value=f"{conn_val} (status={status_data.get('status')})",
                stamp_or_seq=hb_stamp or conn_val,
                stale=veh_stale,
                mode_flag=veh_flag,
                now=now,
            )

            # 4. ToF / ultrasonic distance
            dist_val = raw_sensors.get(
                "front_distance_cm",
                raw_sensors.get("distance_cm", raw_sensors.get("distanceCm")),
            )
            signals["tof_ultrasonic_distance"].record(
                present=dist_val is not None,
                value=dist_val,
                stamp_or_seq=hb_stamp if dist_val is not None else None,
                stale=veh_stale if dist_val is not None else "ABSENT",
                mode_flag=veh_flag,
                now=now,
            )

            # 5. Temperature
            temp_val = raw_sensors.get(
                "temperature",
                raw_sensors.get("temp_c", raw_sensors.get("temperatureC")),
            )
            signals["temperature"].record(
                present=temp_val is not None,
                value=temp_val,
                stamp_or_seq=hb_stamp if temp_val is not None else None,
                stale=veh_stale if temp_val is not None else "ABSENT",
                mode_flag=veh_flag,
                now=now,
            )

            # 6. Gas
            gas_val = raw_sensors.get(
                "gas_level",
                raw_sensors.get("gas_raw", raw_sensors.get("gasRaw")),
            )
            signals["gas"].record(
                present=gas_val is not None,
                value=gas_val,
                stamp_or_seq=hb_stamp if gas_val is not None else None,
                stale=veh_stale if gas_val is not None else "ABSENT",
                mode_flag=veh_flag,
                now=now,
            )

            # 7. Pose
            px = status_data.get("position_x")
            py = status_data.get("position_y")
            palt = status_data.get("position_alt")
            pose_present = px is not None and py is not None
            signals["pose"].record(
                present=pose_present,
                value=f"({px}, {py}, alt={palt})" if pose_present else None,
                stamp_or_seq=hb_stamp or (px, py),
                stale=veh_stale,
                mode_flag=veh_flag,
                now=now,
            )

            # 8. LiDAR
            sensor_health = det_data.get("sensor_health") or {}
            lidar_health = sensor_health.get("lidar") or {}
            lidar_status = lidar_health.get("status", "UNAVAILABLE")
            lidar_enabled = bool(lidar_health.get("enabled"))
            lidar_stamp = lidar_health.get("last_message_timestamp")
            lidar_present = lidar_enabled and lidar_status not in ("UNAVAILABLE", "DISCONNECTED")
            lidar_flag = (
                "REAL"
                if str(lidar_health.get("source_mode", "simulated")).lower() == "physical"
                else "SIMULATED"
            )
            signals["lidar"].record(
                present=lidar_present,
                value=lidar_status if not lidar_present else f"status={lidar_status}, stamp={lidar_stamp}",
                stamp_or_seq=lidar_stamp,
                stale="STALE" if lidar_health.get("is_stale") else lidar_status,
                mode_flag=lidar_flag,
                now=now,
            )

            if time.monotonic() >= deadline:
                break
            sleep_for = min(interval_sec, max(0.0, deadline - time.monotonic()))
            if sleep_for > 0:
                time.sleep(sleep_for)
    finally:
        if client_ctx is not None:
            client_ctx.__exit__(None, None, None)

    return signals


def print_probe_table(signals: dict[str, SignalStats], duration_sec: float) -> None:
    print(f"=== Operation Shadow Read-Only Signal Probe ({duration_sec:.1f}s window) ===")
    header = (
        f"{'Signal':<42} | {'Status':<7} | {'Last Value':<28} | "
        f"{'Rate (Hz)':>9} | {'Staleness':<22} | {'Mode':<9}"
    )
    print(header)
    print("-" * len(header))
    for stats in signals.values():
        status_str = "PRESENT" if stats.present else "ABSENT"
        val_str = str(stats.last_value) if stats.last_value is not None else "—"
        if len(val_str) > 28:
            val_str = val_str[:25] + "..."
        rate_hz = stats.update_rate_hz(duration_sec)
        print(
            f"{stats.name:<42} | {status_str:<7} | {val_str:<28} | "
            f"{rate_hz:>9.2f} | {stats.stale:<22} | {stats.mode_flag:<9}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/signal_probe.py",
        description="Read-only signal probe for Operation Shadow backend.",
    )
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000",
        help="Backend base URL (default: http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=3.0,
        help="Probe duration in seconds (default: 3.0)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Polling interval in seconds (default: 0.5)",
    )
    parser.add_argument(
        "--in-process",
        action="store_true",
        help="Run probe against an in-process FastAPI TestClient (for offline/sandbox verification)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    signals = run_probe(
        base_url=args.url,
        duration_sec=args.duration,
        interval_sec=args.interval,
        in_process=args.in_process,
    )
    if args.json:
        out = {
            k: {
                "present": v.present,
                "last_value": v.last_value,
                "update_rate_hz": v.update_rate_hz(args.duration),
                "staleness": v.stale,
                "mode": v.mode_flag,
                "samples": v.sample_count,
            }
            for k, v in signals.items()
        }
        print(json.dumps(out, indent=2))
    else:
        print_probe_table(signals, args.duration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
