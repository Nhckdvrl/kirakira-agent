"""Typed capability composition with generation-bound borrowing."""

from .scope import ResourceScope
from .services import ServiceKey, ServiceRegistry, ServiceUnavailable

__all__ = ["ResourceScope", "ServiceKey", "ServiceRegistry", "ServiceUnavailable"]
