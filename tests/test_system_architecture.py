from pathlib import Path


def test_trd_section_26_repository_layout():
    """
    Verifies that the root workspace strictly satisfies TRD v2.0 §26 repository layout
    and segregated hardware/frontend/backend structure.
    """
    root = Path(__file__).resolve().parent.parent

    # Top-level directories
    expected_top_level = [
        "ultron",
        "hardware",
        "backend",
        "frontend",
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
        "graph",
        "schemas",
    ]
    for subdir in expected_ultron_dirs:
        dir_path = root / "ultron" / subdir
        assert dir_path.is_dir(), f"Expected ultron subdirectory '{subdir}' missing"

    # scout subdirectories inside hardware/scout (and root symlink if present)
    for subdir in ["navigation", "sensors", "communication"]:
        assert (root / "hardware" / "scout" / subdir).is_dir(), f"Expected hardware/scout/'{subdir}' missing"

    # hawk subdirectories inside hardware/hawk per TRD §26
    for subdir in ["flight_interface", "sensors", "communication"]:
        assert (root / "hardware" / "hawk" / subdir).is_dir(), f"Expected hardware/hawk/'{subdir}' missing"

    # firmware subdirectories inside hardware/firmware/esp32
    for subdir in ["motors", "sensors", "safety"]:
        assert (root / "hardware" / "firmware" / "esp32" / subdir).is_dir(), (
            f"Expected hardware/firmware/esp32/'{subdir}' missing"
        )

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
    assert (root / "hardware" / "hawk").is_dir(), "Expected hardware/hawk directory"
    assert (root / "hardware" / "firmware").is_dir(), "Expected hardware/firmware directory"
