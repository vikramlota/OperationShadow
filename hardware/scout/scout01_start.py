#!/usr/bin/env python3
"""SCOUT01 ROS 2 stack manager. Usage: python3 ~/scout01_start.py start|status|stop"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

HOME = Path.home()
STATE = HOME / ".scout01"
LOGS = STATE / "logs"
PIDS = STATE / "pids"
ROS = Path("/opt/ros/humble/setup.bash")
PICO = HOME / "pico_ws/install/setup.bash"
LIDAR = HOME / "LiDAR_ws/unilidar_sdk2/unitree_lidar_ros2/install/setup.bash"

SERVICES = [
    (
        "oak_camera",
        "OAK-D Pro + spatial YOLO",
        PICO,
        f"ros2 launch depthai_ros_driver_v3 driver.launch.py params_file:={HOME}/yolo_spatial_cool.yaml",
        8,
    ),
    (
        "unitree_lidar",
        "Unitree L2 LiDAR",
        LIDAR,
        "ros2 launch unitree_lidar_ros2 launch.py",
        5,
    ),
    (
        "detections_json",
        "Detection JSON publisher",
        PICO,
        f"python3 {HOME}/detections_json.py",
        2,
    ),
    (
        "detection_markers",
        "Detection marker publisher",
        PICO,
        f"python3 {HOME}/detection_markers.py",
        2,
    ),
    (
        "rosbridge",
        "ROSBridge WebSocket :9090",
        None,
        "ros2 launch rosbridge_server rosbridge_websocket_launch.xml",
        3,
    ),
    (
        "oak_mjpeg",
        "OAK MJPEG HTTP server :8081",
        PICO,
        f"python3 {HOME}/oak_mjpeg_server.py",
        2,
    ),
]


def setup_dirs() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    PIDS.mkdir(parents=True, exist_ok=True)


def pidfile(name: str) -> Path:
    return PIDS / f"{name}.pid"


def get_pid(name: str) -> int | None:
    try:
        return int(pidfile(name).read_text().strip())
    except (OSError, ValueError):
        return None


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def shell_command(extra_setup: Path | None, command: str) -> str:
    prefix = f"source {ROS}"
    if extra_setup:
        prefix += f" && source {extra_setup}"
    return f"{prefix} && exec {command}"


def start_one(item: tuple[str, str, Path | None, str, int]) -> None:
    name, description, extra_setup, command, delay = item
    old_pid = get_pid(name)
    if alive(old_pid):
        print(f"[already running] {name} (PID {old_pid})")
        return
    pidfile(name).unlink(missing_ok=True)
    with open(LOGS / f"{name}.log", "ab", buffering=0) as log:
        proc = subprocess.Popen(
            ["bash", "-lc", shell_command(extra_setup, command)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    pidfile(name).write_text(str(proc.pid))
    print(f"[started] {description} (PID {proc.pid})")
    time.sleep(delay)
    if not alive(proc.pid):
        print(f"[WARNING] {name} exited early; inspect {LOGS / (name + '.log')}")


def start_all() -> None:
    setup_dirs()
    print("Starting SCOUT01 stack. Existing manually-started nodes are NOT killed.")
    for item in SERVICES:
        start_one(item)
    print("\nStartup commands issued.")
    print("Topics: /detections_json, /oak/rgb/image_raw/compressed, /unilidar/cloud")
    print("WebSocket: ws://<SCOUT01-IP>:9090")
    print("MJPEG:     http://<SCOUT01-IP>:8081/stream.mjpg")
    print(f"Logs:      {LOGS}")
    print("Check:     python3 ~/scout01_start.py status")
    print("RViz is intentionally separate: rviz2")


def status() -> None:
    setup_dirs()
    print("SCOUT01 managed processes:")
    for item in SERVICES:
        name = item[0]
        pid = get_pid(name)
        running = alive(pid)
        print(
            f"{name:20} {'RUNNING' if running else 'STOPPED/NOT MANAGED':20} PID={pid or '-'}"
        )
        if not running and pidfile(name).exists():
            pidfile(name).unlink(missing_ok=True)
    print("\nPorts:")
    for port in (9090, 8081):
        result = subprocess.run(
            ["bash", "-lc", f"ss -lnt | grep -q ':{port} '"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"{port}: {'LISTENING' if result.returncode == 0 else 'not listening'}")
    print("\nIPv4:")
    subprocess.run(["hostname", "-I"])


def stop_all() -> None:
    setup_dirs()
    print("Stopping only processes started by this script:")
    for item in reversed(SERVICES):
        name = item[0]
        pid = get_pid(name)
        if not alive(pid):
            print(f"[not running] {name}")
            pidfile(name).unlink(missing_ok=True)
            continue
        try:
            os.killpg(pid, signal.SIGINT)
            print(f"[stop requested] {name}")
        except ProcessLookupError:
            pass
        deadline = time.time() + 4
        while time.time() < deadline and alive(pid):
            time.sleep(0.2)
        if alive(pid):
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        pidfile(name).unlink(missing_ok=True)
    print("Stop requests sent. Manually launched terminals are untouched.")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "status", "stop"])
    action = parser.parse_args().action
    if not ROS.exists():
        sys.exit(f"ROS setup not found: {ROS}")
    if action == "start":
        start_all()
    elif action == "status":
        status()
    else:
        stop_all()


if __name__ == "__main__":
    main()
