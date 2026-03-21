"""Shared Pydantic models for JARVIS microservices."""

from pydantic import BaseModel


class HealthCheckResponse(BaseModel):
    """Standard health check response for all services."""
    status: str
    service: str
    checks: dict[str, str]
