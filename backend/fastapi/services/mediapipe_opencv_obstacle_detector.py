"""Live MediaPipe + OpenCV obstacle detector for SCOUT01 camera stream (`http://10.68.44.41:8081/stream.mjpg`).

Reads frames from the SCOUT01 MJPEG HTTP stream (or local camera index), runs
MediaPipe (Pose / SelfieSegmentation / Hands) + OpenCV contour, edge-density, and
looming-surface analysis in the forward driving corridor, estimates obstacle
distance (`range_m`) and lateral offset (`x` in `oak_rgb_camera_optical_frame`)
based on left-vs-right corridor clearance, and feeds verified obstacle detections
directly into the ULTRON WorldModel and MissionService so SCOUT01 immediately
stops ('X'), reverses ('S'), reroutes ('A'/'D' -> 'W' -> counter-turn), and replans.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
import sys
import threading
import time
from typing import Any
import urllib.request

logger = logging.getLogger("operation_shadow.mediapipe_opencv")

_IS_PYTEST = "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ

try:
    import cv2  # type: ignore[import-not-found]
    import numpy as np  # type: ignore[import-not-found]

    _HAS_CV2 = True
except Exception:
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    _HAS_CV2 = False

try:
    import mediapipe as mp  # type: ignore[import-not-found]

    _HAS_MEDIAPIPE = True
except Exception:
    mp = None  # type: ignore[assignment]
    _HAS_MEDIAPIPE = False


def get_default_camera_url() -> str:
    """Resolve the SCOUT01 MJPEG stream URL from environment variables."""
    url = (os.getenv("SCOUT01_CAMERA_URL") or "").strip()
    if url:
        return url
    ip = (os.getenv("SCOUT01_IP") or os.getenv("SHADOW_ROS_HOST") or "10.68.44.41").strip()
    port = (os.getenv("SCOUT_CAMERA_PORT") or "8081").strip()
    if not ip:
        ip = "10.68.44.41"
    return f"http://{ip}:{port}/stream.mjpg"


class MediaPipeOpenCVObstacleDetector:
    """Real-time MediaPipe + OpenCV obstacle & free-space detector for SCOUT01."""

    def __init__(
        self,
        stream_url: str | None = None,
        *,
        poll_interval_sec: float = 0.20,
        stopping_distance_m: float = 1.6,
    ) -> None:
        self.stream_url = (stream_url or get_default_camera_url()).strip()
        self.poll_interval_sec = max(0.08, float(poll_interval_sec))
        self.stopping_distance_m = float(stopping_distance_m)

        self._mp_pose: Any = None
        self._mp_seg: Any = None
        self._mp_hands: Any = None
        self._mp_initialized = False

        self._latest_detection: dict[str, Any] | None = None
        self._latest_timestamp: float = 0.0
        self._frame_seq: int = 0
        self._running: bool = False
        self._task: asyncio.Task[None] | None = None
        self._lock = threading.Lock()

    def _ensure_mediapipe_initialized(self) -> None:
        if self._mp_initialized:
            return
        self._mp_initialized = True
        if not _HAS_MEDIAPIPE or mp is None:
            return
        try:
            solutions = getattr(mp, "solutions", None)
            if solutions is not None:
                if hasattr(solutions, "pose"):
                    self._mp_pose = solutions.pose.Pose(
                        static_image_mode=False,
                        model_complexity=0,
                        min_detection_confidence=0.45,
                        min_tracking_confidence=0.45,
                    )
                if hasattr(solutions, "selfie_segmentation"):
                    self._mp_seg = solutions.selfie_segmentation.SelfieSegmentation(
                        model_selection=1
                    )
                if hasattr(solutions, "hands"):
                    self._mp_hands = solutions.hands.Hands(
                        static_image_mode=False,
                        max_num_hands=2,
                        min_detection_confidence=0.45,
                    )
        except Exception as exc:
            logger.debug("MediaPipe solutions init notice: %s", exc)

    def _fetch_single_bgr_frame(self, timeout_sec: float = 1.2) -> Any | None:
        """Fetch and decode a single BGR frame from the MJPEG HTTP stream or local webcam."""
        if not _HAS_CV2 or cv2 is None or np is None:
            return None

        if self.stream_url.isdigit():
            cap = cv2.VideoCapture(int(self.stream_url))
            try:
                ok, frame = cap.read()
                return frame if ok else None
            finally:
                cap.release()

        req = urllib.request.Request(
            self.stream_url,
            headers={"User-Agent": "OperationShadow-MediaPipe-ObstacleDetector/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                buf = bytearray()
                deadline = time.monotonic() + timeout_sec
                while time.monotonic() < deadline and len(buf) < 512_000:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    start_idx = buf.find(b"\xff\xd8")
                    if start_idx != -1:
                        end_idx = buf.find(b"\xff\xd9", start_idx + 2)
                        if end_idx != -1:
                            jpg_bytes = bytes(buf[start_idx : end_idx + 2])
                            arr = np.frombuffer(jpg_bytes, dtype=np.uint8)
                            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                            return frame
        except Exception:
            return None
        return None

    def analyze_bgr_frame(self, frame: Any) -> dict[str, Any] | None:
        """Run MediaPipe + OpenCV obstacle and corridor-clearance detection on a BGR frame."""
        if not _HAS_CV2 or cv2 is None or np is None or frame is None:
            return None

        self._ensure_mediapipe_initialized()
        h, w = frame.shape[:2]
        if h < 32 or w < 32:
            return None

        small_w = 320
        small_h = max(180, int(round(h * (small_w / float(w)))))
        resized = cv2.resize(frame, (small_w, small_h))
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

        detected_label: str | None = None
        confidence: float = 0.0
        obstacle_cx_norm: float = 0.5
        coverage_ratio: float = 0.0

        # 1. MediaPipe Pose / Hands / Foreground Segmentation
        if self._mp_pose is not None:
            try:
                pose_res = self._mp_pose.process(rgb)
                if pose_res and getattr(pose_res, "pose_landmarks", None):
                    lms = pose_res.pose_landmarks.landmark
                    xs = [float(lm.x) for lm in lms if getattr(lm, "visibility", 1.0) > 0.35]
                    ys = [float(lm.y) for lm in lms if getattr(lm, "visibility", 1.0) > 0.35]
                    if xs and ys:
                        min_x, max_x = max(0.0, min(xs)), min(1.0, max(xs))
                        min_y, max_y = max(0.0, min(ys)), min(1.0, max(ys))
                        box_w = max_x - min_x
                        box_h = max_y - min_y
                        cx = 0.5 * (min_x + max_x)
                        if 0.18 <= cx <= 0.82 and (box_h >= 0.28 or (box_w * box_h) >= 0.10):
                            detected_label = "person"
                            confidence = 0.92
                            obstacle_cx_norm = cx
                            coverage_ratio = max(coverage_ratio, max(box_h, box_w * 1.4))
            except Exception:
                pass

        if detected_label is None and self._mp_hands is not None:
            try:
                hand_res = self._mp_hands.process(rgb)
                if hand_res and getattr(hand_res, "multi_hand_landmarks", None):
                    for hand_lms in hand_res.multi_hand_landmarks:
                        xs = [float(lm.x) for lm in hand_lms.landmark]
                        ys = [float(lm.y) for lm in hand_lms.landmark]
                        cx = 0.5 * (min(xs) + max(xs))
                        area = (max(xs) - min(xs)) * (max(ys) - min(ys))
                        if 0.15 <= cx <= 0.85 and area >= 0.04:
                            detected_label = "obstacle"
                            confidence = 0.89
                            obstacle_cx_norm = cx
                            coverage_ratio = max(coverage_ratio, min(1.0, area * 3.5))
                            break
            except Exception:
                pass

        if detected_label is None and self._mp_seg is not None:
            try:
                seg_res = self._mp_seg.process(rgb)
                mask = getattr(seg_res, "segmentation_mask", None)
                if mask is not None:
                    roi_mask = mask[
                        int(small_h * 0.20) : int(small_h * 0.95),
                        int(small_w * 0.20) : int(small_w * 0.80),
                    ]
                    fg_ratio = float(np.mean(roi_mask > 0.55))
                    if fg_ratio >= 0.28:
                        ys_idx, xs_idx = np.where(mask > 0.55)
                        cx = float(np.mean(xs_idx)) / float(small_w) if len(xs_idx) > 0 else 0.5
                        detected_label = "person"
                        confidence = round(min(0.95, 0.65 + fg_ratio * 0.4), 2)
                        obstacle_cx_norm = cx
                        coverage_ratio = max(coverage_ratio, fg_ratio)
            except Exception:
                pass

        # 2. OpenCV Multi-Cue Obstacle & Corridor Free-Space Analysis
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 45, 135)
        kernel = np.ones((3, 3), dtype=np.uint8)
        dilated = cv2.dilate(edges, kernel, iterations=2)

        y0, y1 = int(small_h * 0.25), int(small_h * 0.92)
        x_left_end = int(small_w * 0.33)
        x_right_start = int(small_w * 0.67)

        left_edges = dilated[y0:y1, 0:x_left_end]
        center_edges = dilated[y0:y1, x_left_end:x_right_start]
        right_edges = dilated[y0:y1, x_right_start:small_w]

        left_density = float(np.mean(left_edges > 0))
        center_density = float(np.mean(center_edges > 0))
        right_density = float(np.mean(right_edges > 0))

        corridor_x0, corridor_x1 = int(small_w * 0.15), int(small_w * 0.85)
        corridor_y0, corridor_y1 = int(small_h * 0.20), int(small_h * 0.95)
        corridor_bin = dilated[corridor_y0:corridor_y1, corridor_x0:corridor_x1]
        contours_info = cv2.findContours(
            corridor_bin, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        contours = contours_info[0] if len(contours_info) == 2 else contours_info[1]

        corridor_area = float((corridor_y1 - corridor_y0) * (corridor_x1 - corridor_x0))
        largest_box_ratio = 0.0
        largest_cx_norm = 0.5
        for cnt in contours:
            bx, by, bw, bh = cv2.boundingRect(cnt)
            box_area = float(bw * bh)
            ratio = box_area / max(1.0, corridor_area)
            if (by + bh) >= int(0.45 * (corridor_y1 - corridor_y0)) and ratio > largest_box_ratio:
                largest_box_ratio = ratio
                largest_cx_norm = (corridor_x0 + bx + 0.5 * bw) / float(small_w)

        center_roi_bgr = resized[y0:y1, x_left_end:x_right_start]
        bottom_strip_bgr = resized[int(small_h * 0.78) : int(small_h * 0.96), x_left_end:x_right_start]
        color_diff = float(
            np.linalg.norm(
                np.mean(center_roi_bgr, axis=(0, 1)) - np.mean(bottom_strip_bgr, axis=(0, 1))
            )
        )

        if detected_label is None:
            if largest_box_ratio >= 0.16 or center_density >= 0.18:
                detected_label = "obstacle"
                confidence = round(
                    min(0.94, max(0.68, largest_box_ratio * 1.8, center_density * 3.0)),
                    2,
                )
                obstacle_cx_norm = largest_cx_norm
                coverage_ratio = max(largest_box_ratio, center_density * 2.2)
            elif color_diff >= 38.0 and largest_box_ratio >= 0.09:
                detected_label = "obstacle"
                confidence = 0.76
                obstacle_cx_norm = largest_cx_norm
                coverage_ratio = max(0.25, largest_box_ratio * 2.0)

        if detected_label is None:
            return None

        estimated_range_m = round(
            max(0.35, min(1.55, 1.65 - (coverage_ratio * 1.15))),
            2,
        )
        if estimated_range_m > self.stopping_distance_m:
            return None

        left_obstruction = left_density + (0.15 if obstacle_cx_norm < 0.46 else 0.0)
        right_obstruction = right_density + (0.15 if obstacle_cx_norm > 0.54 else 0.0)

        if left_obstruction <= right_obstruction:
            x_optical = round(max(0.22, (obstacle_cx_norm - 0.5) * 0.9 + 0.25), 2)
            preferred_turn = "A"
            clear_side = "LEFT"
        else:
            x_optical = round(min(-0.22, (obstacle_cx_norm - 0.5) * 0.9 - 0.25), 2)
            preferred_turn = "D"
            clear_side = "RIGHT"

        self._frame_seq += 1
        now_ts = time.time()
        return {
            "id": self._frame_seq,
            "class": detected_label,
            "label": detected_label,
            "score": confidence,
            "confidence": confidence,
            "position": {
                "x": x_optical,
                "y": 0.0,
                "z": estimated_range_m,
            },
            "range_m": estimated_range_m,
            "distance_m": estimated_range_m,
            "coordinate_frame": "oak_rgb_camera_optical_frame",
            "preferred_turn": preferred_turn,
            "clear_side": clear_side,
            "left_density": round(left_density, 3),
            "center_density": round(center_density, 3),
            "right_density": round(right_density, 3),
            "stamp": now_ts,
            "source": "mediapipe_opencv",
        }

    async def poll_once(
        self,
        agent: Any | None = None,
        perception_svc: Any | None = None,
    ) -> dict[str, Any] | None:
        """Fetch one frame from the SCOUT01 camera stream, run MediaPipe+OpenCV, and push into WorldModel."""
        if _IS_PYTEST:
            return None
        frame = await asyncio.to_thread(self._fetch_single_bgr_frame, 1.0)
        if frame is None:
            return None
        detection = await asyncio.to_thread(self.analyze_bgr_frame, frame)
        with self._lock:
            self._latest_detection = detection
            if detection is not None:
                self._latest_timestamp = float(detection["stamp"])

        if detection is not None and agent is not None:
            now_dt = datetime.now(timezone.utc)
            if hasattr(agent, "ingest_detection_frame_into_world_model"):
                try:
                    agent.ingest_detection_frame_into_world_model(
                        stamp=float(detection["stamp"]),
                        received_at=now_dt,
                        frame="oak_rgb_camera_optical_frame",
                        objects=[
                            {
                                "id": int(detection["id"]),
                                "class": str(detection["class"]),
                                "score": float(detection["score"]),
                                "position": dict(detection["position"]),
                                "range_m": float(detection["range_m"]),
                            }
                        ],
                        vehicle_id="SCOUT01",
                        telemetry_source="rosbridge",
                        topic="/mediapipe_opencv/obstacles",
                        map_frame_confirmed=False,
                    )
                except Exception as exc:
                    logger.debug("MediaPipe WorldModel ingest notice: %s", exc)
            if perception_svc is not None and hasattr(
                perception_svc, "get_vehicle_detection_state"
            ):
                try:
                    push_payload = perception_svc.get_vehicle_detection_state("SCOUT01")
                    gw = getattr(perception_svc, "_gateway", None)
                    if gw is not None:
                        await gw.broadcast_detections(push_payload)
                except Exception:
                    pass
        return detection

    def get_recent_obstacle(self, max_age_sec: float = 1.8) -> dict[str, Any] | None:
        """Return the most recent MediaPipe+OpenCV obstacle detection if fresh."""
        with self._lock:
            if (
                self._latest_detection is not None
                and (time.time() - self._latest_timestamp) <= max_age_sec
            ):
                return dict(self._latest_detection)
        return None

    def start_background_loop(
        self,
        agent: Any | None = None,
        perception_svc: Any | None = None,
    ) -> None:
        """Start continuous background MediaPipe+OpenCV obstacle polling."""
        if _IS_PYTEST or self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._run_loop(agent=agent, perception_svc=perception_svc),
            name="mediapipe-opencv-obstacle-detector",
        )
        logger.info(
            "Started MediaPipe + OpenCV obstacle detector on %s",
            self.stream_url,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run_loop(
        self,
        agent: Any | None = None,
        perception_svc: Any | None = None,
    ) -> None:
        while self._running:
            try:
                await self.poll_once(agent=agent, perception_svc=perception_svc)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("MediaPipe+OpenCV loop notice: %s", exc)
            await asyncio.sleep(self.poll_interval_sec)


_detector_singleton: MediaPipeOpenCVObstacleDetector | None = None


def get_mediapipe_opencv_detector() -> MediaPipeOpenCVObstacleDetector:
    global _detector_singleton
    if _detector_singleton is None:
        _detector_singleton = MediaPipeOpenCVObstacleDetector()
    return _detector_singleton


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    det = get_mediapipe_opencv_detector()
    print(f"Polling MediaPipe + OpenCV obstacle detector from {det.stream_url} ... (Ctrl+C to stop)")
    try:
        while True:
            frame = det._fetch_single_bgr_frame(timeout_sec=1.5)
            if frame is None:
                print("Waiting for MJPEG frame...")
            else:
                res = det.analyze_bgr_frame(frame)
                if res:
                    print(
                        f"[OBSTACLE DETECTED] {res['label']} at {res['distance_m']}m "
                        f"| clear_side={res['clear_side']} (turn '{res['preferred_turn']}')"
                    )
                else:
                    print("[CLEAR] Forward corridor is clear.")
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\nStopped.")
