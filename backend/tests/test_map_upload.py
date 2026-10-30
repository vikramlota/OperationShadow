"""Tests for the 3D SLAM map upload endpoint (/upload-map/{vehicle_id})."""

from __future__ import annotations

import io
from pathlib import Path
import pytest
from starlette.testclient import TestClient

from main import create_app
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, AsyncSession


def test_upload_map_endpoint_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    app = create_app()
    client = TestClient(app)

    files = {
        "visual_map.ply": ("visual_map.ply", io.BytesIO(b"PLY_DATA_HEADER_3D"), "application/octet-stream"),
        "lidar_map.pcd": ("lidar_map.pcd", io.BytesIO(b"PCD_DATA_HEADER_LIDAR"), "application/octet-stream"),
        "rtabmap.db": ("rtabmap.db", io.BytesIO(b"SQLITE_FORMAT_3_DB"), "application/octet-stream"),
    }

    resp = client.post("/upload-map/SCOUT01", files=files)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["vehicle_id"] == "SCOUT01"
    assert "visual_map.ply" in data["saved_files"]
    assert "lidar_map.pcd" in data["saved_files"]
    assert "rtabmap.db" in data["saved_files"]

    saved_dir = tmp_path / "saved_maps"
    assert saved_dir.is_dir()
    assert (saved_dir / "visual_map.ply").read_bytes() == b"PLY_DATA_HEADER_3D"
    assert (saved_dir / "lidar_map.pcd").read_bytes() == b"PCD_DATA_HEADER_LIDAR"
    assert (saved_dir / "rtabmap.db").read_bytes() == b"SQLITE_FORMAT_3_DB"


def test_upload_map_optional_single_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    app = create_app()
    client = TestClient(app)

    files = {
        "visual_map": ("visual_map.ply", io.BytesIO(b"ONLY_PLY"), "application/octet-stream"),
    }

    resp = client.post("/upload-map/SCOUT01", files=files)
    assert resp.status_code == 200
    data = resp.json()
    assert data["vehicle_id"] == "SCOUT01"
    assert data["saved_files"] == ["visual_map.ply"]

    saved_dir = tmp_path / "saved_maps"
    assert (saved_dir / "visual_map.ply").read_bytes() == b"ONLY_PLY"
    assert not (saved_dir / "lidar_map.pcd").exists()


def test_upload_map_invalid_vehicle_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    app = create_app()
    client = TestClient(app)

    resp = client.post("/upload-map/bad!vehicle@id", files={"visual_map": ("visual_map.ply", io.BytesIO(b"abc"))})
    assert resp.status_code == 400
