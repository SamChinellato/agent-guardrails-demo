"""Shared plumbing for the demo services: JSON logs carrying trace context, OTel setup,
and the /healthz, /readyz and /metrics endpoints every service exposes (NFR-06)."""

from demo_common.app import create_app, run
from demo_common.logs import get_logger

__all__ = ["create_app", "run", "get_logger"]
