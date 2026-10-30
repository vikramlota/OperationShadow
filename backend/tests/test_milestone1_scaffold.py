"""Milestone 1 backend verification tests.

Verifies:
1. FastAPI /health endpoint responds with 200 OK and expected safety defaults.
2. Asynchronous SQLite (SQLAlchemy 2.0 + aiosqlite) connection and init_db work.
3. Hardware lockout prevents activating 'real' vehicle mode while enabled.
4. Shared-secret vehicle authentication accepts valid tokens and rejects invalid ones.
5. Isolated ULTRON stub satisfies UltronAgentPort protocol.
"""

from __future__ import annotations

from core.config import Settings, settings
from core.security import is_valid_vehicle_token, verify_vehicle_token
from db.session import check_db_connection, init_db
from fastapi import Depends, HTTPException
from httpx import ASGITransport, AsyncClient
from main import app
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from ultron_stub import StubUltronAgent, UltronAgentPort


async def test_health_endpoint_returns_expected_defaults() -> None:
    """Verify the minimal runnable FastAPI app serves /health with safety defaults."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["database"] == "ok"
    assert payload["hardware_lockout"] is True
    assert payload["default_vehicle_mode"] == "simulated"
    assert payload["ultron_is_stub"] is True
    assert payload["vehicles"] == ["HAWK01", "SCOUT01"]


async def test_async_sqlite_engine_connectivity() -> None:
    """Verify SQLAlchemy 2.0 + aiosqlite connects and initializes cleanly in memory."""
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    try:
        await init_db(test_engine)
        assert await check_db_connection(test_engine) is True
    finally:
        await test_engine.dispose()


def test_hardware_lockout_rejects_real_mode() -> None:
    """Verify hardware lockout blocks real vehicle mode until specs are confirmed."""
    with pytest.raises(ValueError, match="hardware_lockout"):
        Settings(hardware_lockout=True, default_vehicle_mode="real")

    locked_settings = Settings(hardware_lockout=True, default_vehicle_mode="simulated")
    locked_settings.validate_requested_mode("simulated")
    with pytest.raises(ValueError, match="Real hardware mode is locked"):
        locked_settings.validate_requested_mode("real")


async def test_shared_secret_vehicle_token_validation() -> None:
    """Verify constant-time token comparison and FastAPI security dependency."""
    expected = settings.vehicle_shared_secret
    assert is_valid_vehicle_token(expected) is True
    assert is_valid_vehicle_token("wrong-token") is False
    assert is_valid_vehicle_token(None) is False

    # Direct header match
    verified = await verify_vehicle_token(
        x_vehicle_token=expected,
        authorization=None,
        token=None,
    )
    assert verified == expected

    # Bearer token match
    verified_bearer = await verify_vehicle_token(
        x_vehicle_token=None,
        authorization=f"Bearer {expected}",
        token=None,
    )
    assert verified_bearer == expected

    # Invalid token raises 401
    with pytest.raises(HTTPException) as exc_info:
        await verify_vehicle_token(
            x_vehicle_token="invalid-secret",
            authorization=None,
            token=None,
        )
    assert exc_info.value.status_code == 401
    assert Depends(verify_vehicle_token) is not None


async def test_ultron_stub_protocol_compliance() -> None:
    """Verify StubUltronAgent satisfies UltronAgentPort without real planning logic."""
    agent = StubUltronAgent()
    assert isinstance(agent, UltronAgentPort)
    assert agent.is_stub_implementation() is True

    snapshot = await agent.get_world_model_snapshot()
    assert snapshot.is_stub is True
    assert snapshot.active_mission_id is None
    assert "SCOUT01" in snapshot.vehicles
    assert "HAWK01" in snapshot.vehicles
