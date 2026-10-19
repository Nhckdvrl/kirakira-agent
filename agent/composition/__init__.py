"""Compatibility imports for the native kernel and distribution adapter."""

from .root import CompositionRoot, Context, Module
from .scope import ResourceScope
from .services import ServiceKey, ServiceRegistry, ServiceUnavailable

__all__ = [
    "ResourceScope",
    "ServiceKey",
    "ServiceRegistry",
    "ServiceUnavailable",
    "CompositionRoot",
    "Context",
    "Module",
]
