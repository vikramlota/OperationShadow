#!/usr/bin/env bash
# Launch or manage the SCOUT01 ROS 2 stack (OAK-D Pro + Unitree L2 LiDAR + ROSBridge :9090 + MJPEG :8081)
# over SSH on chawlaji@10.68.44.41 with local port forwarding for :9090 and :8081.
set -euo pipefail

SCOUT_SSH_TARGET="${SCOUT_SSH_TARGET:-chawlaji@10.68.44.41}"
ACTION="${1:-start}"

case "$ACTION" in
  start)
    echo "Connecting to ${SCOUT_SSH_TARGET}, starting SCOUT01 stack via ~/scout01_start.py, and forwarding :9090 (ROSBridge) & :8081 (MJPEG)..."
    exec ssh \
      -o ExitOnForwardFailure=no \
      -L 9090:127.0.0.1:9090 \
      -L 8081:127.0.0.1:8081 \
      -t "${SCOUT_SSH_TARGET}" \
      "python3 ~/scout01_start.py start && echo '' && python3 ~/scout01_start.py status && echo '' && echo 'SSH tunnel active (:9090 -> ROSBridge, :8081 -> OAK MJPEG). Press Ctrl+D or type exit to close tunnel.' && exec bash -l"
    ;;
  status)
    exec ssh "${SCOUT_SSH_TARGET}" "python3 ~/scout01_start.py status"
    ;;
  stop)
    exec ssh "${SCOUT_SSH_TARGET}" "python3 ~/scout01_start.py stop"
    ;;
  tunnel)
    echo "Opening SSH port-forward tunnel (:9090 -> ROSBridge, :8081 -> OAK MJPEG) to ${SCOUT_SSH_TARGET}..."
    exec ssh -N \
      -L 9090:127.0.0.1:9090 \
      -L 8081:127.0.0.1:8081 \
      "${SCOUT_SSH_TARGET}"
    ;;
  *)
    echo "Usage: $0 {start|status|stop|tunnel}"
    exit 1
    ;;
esac
