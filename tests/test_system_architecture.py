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
        "hawk",
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

    # hawk subdirectories specified in TRD §26
    for subdir in ["flight_interface", "sensors", "communication"]:
        assert (root / "hawk" / subdir).is_dir(), f"Expected hawk subdirectory '{subdir}' missing"

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
