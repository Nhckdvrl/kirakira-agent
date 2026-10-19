"""Read-only application view of the native composition service directory."""

from contextlib import asynccontextmanager

from agent.plugin_composition.model import ServiceKey as ServiceKey


class ServiceUnavailable(LookupError):
    pass


class ServiceRegistry:
    """Compatibility view: the native Root is the sole registration authority."""

    def __init__(self, root):
        self.root = root

    def resolve(self, key):
        values = self.root.provided_services()
        if key not in values:
            raise ServiceUnavailable("missing capability: " + key.name)
        return values[key]

    def contains(self, key):
        try:
            self.resolve(key)
            return True
        except ServiceUnavailable:
            return False

    @asynccontextmanager
    async def use(self, key):
        try:
            context, value = self.root._service_provider(key)
        except (LookupError, RuntimeError) as error:
            raise ServiceUnavailable("missing capability: " + key.name) from error
        with context.call_scope():
            yield value

    def catalog(self):
        return tuple(
            {"key": key.name, "owner": owner}
            for key, owner in self.root.plugin_service_owners().items()
        )
