"""Capability names belong to providers; the host only publishes and leases."""

import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


class ServiceUnavailable(LookupError):
    pass


@dataclass(frozen=True)
class ServiceKey(Generic[T]):
    name: str
    value_type: type | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9_.-]*\.v[1-9][0-9]*", self.name):
            raise ValueError("service keys require a domain name and explicit version")


@dataclass(frozen=True)
class _Binding:
    owner: str
    generation: object | None
    value: object


class ServiceRegistry:
    def __init__(self):
        self._bindings: dict[str, _Binding] = {}
        self._contracts = {}

    def validate(self, owner, values):
        names = set()
        for key, value in values.items():
            if not isinstance(key, ServiceKey):
                raise TypeError("provided capabilities must use ServiceKey")
            if key.name in names:
                raise ValueError("duplicate capability name: " + key.name)
            names.add(key.name)
            if (
                key.name in self._contracts
                and self._contracts[key.name] is not key.value_type
            ):
                raise TypeError(
                    "capability ABI changed without a new version: " + key.name
                )
            if key.value_type is not None and not isinstance(value, key.value_type):
                raise TypeError("invalid implementation for " + key.name)
            previous = self._bindings.get(key.name)
            if previous is not None and previous.owner != owner:
                raise ValueError(
                    f"capability {key.name} is already owned by {previous.owner}"
                )

    def publish(self, owner, values, *, generation=None):
        self.validate(owner, values)
        updated = {
            name: binding
            for name, binding in self._bindings.items()
            if binding.owner != owner
        }
        updated.update(
            {
                key.name: _Binding(owner, generation, value)
                for key, value in values.items()
            }
        )
        self._contracts.update({key.name: key.value_type for key in values})
        self._bindings = updated

    def withdraw(self, owner, *, generation=None):
        self._bindings = {
            name: binding
            for name, binding in self._bindings.items()
            if not (
                binding.owner == owner
                and (generation is None or binding.generation is generation)
            )
        }

    @asynccontextmanager
    async def use(self, key: ServiceKey[T]):
        binding = self._bindings.get(key.name)
        if binding is None:
            raise ServiceUnavailable("missing capability: " + key.name)
        if key.value_type is not None and not isinstance(binding.value, key.value_type):
            raise TypeError("consumer contract differs for " + key.name)
        generation = binding.generation
        if generation is not None:
            generation.acquire()
        try:
            yield binding.value
        finally:
            if generation is not None:
                generation.release()

    def contains(self, key):
        return key.name in self._bindings

    def catalog(self):
        return tuple(
            {
                "key": name,
                "owner": value.owner,
                "generation": getattr(value.generation, "generation_id", None),
            }
            for name, value in sorted(self._bindings.items())
        )
