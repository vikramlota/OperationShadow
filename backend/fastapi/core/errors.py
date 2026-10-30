"""Consistent application error hierarchy and FastAPI exception handlers."""

from __future__ import annotations

from typing import Any

from core.logging import get_logger
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from schemas.errors import ErrorDetail, ErrorResponse

logger = get_logger("errors")


class ShadowAppError(Exception):
    """Base application exception with a standardized HTTP status and error_code."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "SHADOW_ERROR",
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: dict[str, Any] | list[ErrorDetail] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.details = details


class ResourceNotFoundError(ShadowAppError):
    """Raised when a requested Mission, Vehicle, or Task entity does not exist."""

    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(
            f"{resource} '{identifier}' was not found.",
            error_code="RESOURCE_NOT_FOUND",
            status_code=status.HTTP_404_NOT_FOUND,
            details={"resource": resource, "id": identifier},
        )


class HardwareLockoutError(ShadowAppError):
    """Raised when attempting to engage real hardware mode while lockout is active."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or (
                "Real hardware control is locked (SHADOW_HARDWARE_LOCKOUT=true). "
                "Confirm vehicle interface and safety specifications first."
            ),
            error_code="HARDWARE_LOCKOUT_ACTIVE",
            status_code=status.HTTP_409_CONFLICT,
        )


class ProtocolValidationError(ShadowAppError):
    """Raised when a vehicle or WebSocket message violates structural contracts."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            error_code="PROTOCOL_VALIDATION_ERROR",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            details=details,
        )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach consistent ErrorResponse JSON handlers to the FastAPI application."""

    @app.exception_handler(ShadowAppError)
    async def handle_shadow_error(request: Request, exc: ShadowAppError) -> JSONResponse:
        logger.warning(
            exc.message,
            extra={"error_code": exc.error_code, "request_path": request.url.path},
        )
        body = ErrorResponse(
            error_code=exc.error_code,
            message=exc.message,
            details=exc.details,
            request_path=request.url.path,
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"))

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        body = ErrorResponse(
            error_code=f"HTTP_{exc.status_code}",
            message=str(exc.detail),
            request_path=request.url.path,
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            ErrorDetail(
                loc=[
                    item if isinstance(item, int) else str(item)
                    for item in err.get("loc", ())
                ],
                message=str(err.get("msg", "Invalid value")),
                error_type=str(err.get("type", "value_error")),
            )
            for err in exc.errors()
        ]
        body = ErrorResponse(
            error_code="VALIDATION_ERROR",
            message="Request payload validation failed.",
            details=details,
            request_path=request.url.path,
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=body.model_dump(mode="json"),
        )
