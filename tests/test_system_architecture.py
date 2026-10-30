import os
from pathlib import Path


def test_trd_section_26_repository_layout():
    """
    Verifies that the root workspace strictly satisfies TRD v2.0 §26 repository layout.
    """
    root = Path(__file__).resolve().parent.parent

    # Top-level directories specified in TRD §26
    expected_top_level = [
        "ultron",
        "scout",
        "firmware",
        "backend",
        "dashboard",
        "configs",
        "tests",
        "docs",
    ]
    for directory in expected_top_level:
        dir_path = root / directory
        assert dir_path.is_dir(), f"Expected top-level directory '{directory}' missing from repository"

    # ultron subdirectories specified in TRD §26
    expected_ultron_dirs = [
        "planner",
        "memory",
        "world_model",
        "replanner",
        "vision",
        "communication",
        "safety",
        "simulation",
    ]
    for subdir in expected_ultron_dirs:
        dir_path = root / "ultron" / subdir
        assert dir_path.is_dir(), f"Expected ultron subdirectory '{subdir}' missing"

    # scout subdirectories specified in TRD §26
    for subdir in ["navigation", "sensors", "communication"]:
        assert (root / "scout" / subdir).is_dir(), f"Expected scout subdirectory '{subdir}' missing"

    # firmware subdirectories specified in TRD §26
    for subdir in ["motors", "sensors", "safety"]:
        assert (root / "firmware" / "esp32" / subdir).is_dir(), f"Expected firmware/esp32/'{subdir}' missing"

    # product documents in docs/
    for doc in [
        "Operation Shadow — PRD v2.0.md",
        "Operation Shadow — TRD v2.0.md",
        "Operation Shadow — ULTRON Agent Architecture.md",
        "Operation Shadow — Backend & Frontend Architecture.md",
    ]:
        assert (root / "docs" / doc).is_file(), f"Expected document '{doc}' missing from docs/"


def test_segregated_frontend_backend_hardware_layout():
    """
    Verifies that the workspace maintains clean segregation between frontend,
    backend, and hardware layers.
    """
    root = Path(__file__).resolve().parent.parent

    # Segregated top-level directories
    assert (root / "frontend").is_dir(), "Expected top-level 'frontend/' directory"
    assert (root / "frontend" / "package.json").is_file(), "Expected frontend/package.json"
    assert (root / "frontend" / "src").is_dir(), "Expected frontend/src directory"

    assert (root / "backend").is_dir(), "Expected top-level 'backend/' directory"
    assert (root / "backend" / "pyproject.toml").is_file(), "Expected backend/pyproject.toml"
    assert (root / "backend" / "fastapi").is_dir(), "Expected backend/fastapi directory"

    assert (root / "hardware").is_dir(), "Expected top-level 'hardware/' directory"
    assert (root / "hardware" / "scout").is_dir(), "Expected hardware/scout directory"
    assert (root / "hardware" / "firmware").is_dir(), "Expected hardware/firmware directory"

