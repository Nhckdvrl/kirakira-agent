import re
from datetime import UTC, datetime

from agent.composition import ServiceKey
from plugins.ledger.owner_store import StateConflict

PROJECTS = ServiceKey("projects.v1")
OWNER = "projects"


def project_id(value):
    if not isinstance(value, str) or re.fullmatch(r"p_[0-9a-f]{32}", value) is None:
        raise ValueError(
            "project_id must be p_ followed by 32 lowercase hex characters"
        )
    return value


def project_name(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80:
        raise ValueError("project name must contain 1..80 characters")
    return value.strip()


class Projects:
    def __init__(self, store, directories):
        self.store, self.directories = store, directories

    @staticmethod
    def _row(identity, record):
        return {
            "id": identity,
            "revision": record.version,
            **{
                key: record.value[key]
                for key in ("name", "archived", "created_at", "directory")
            },
        }

    def list(self):
        return sorted(
            [self._row(key, record) for key, record in self.store.scan(OWNER)],
            key=lambda row: row["created_at"],
        )

    def create(self, identity, name, directory=None):
        identity, name = project_id(identity), project_name(name)
        # A retry is a read of the original receipt, even if its directory vanished.
        with self.store.transaction() as tx:
            old = tx.read(OWNER, identity)
            if old is not None:
                if (old.value["created_name"], old.value["created_directory"]) != (
                    name,
                    directory,
                ):
                    raise StateConflict(
                        "project ID already has different creation arguments"
                    )
                return self._row(identity, old)
            if directory is not None:
                canonical = str(self.directories.check_absolute(directory))
            else:
                canonical = None
            value = {
                "name": name,
                "created_name": name,
                "created_directory": directory,
                "directory": canonical,
                "archived": False,
                "created_at": datetime.now(UTC).isoformat(),
            }
            return self._row(
                identity, tx.save(OWNER, identity, value, expected_version=None)
            )

    def update(self, identity, *, name=None, archived=None, expected_version=None):
        identity = project_id(identity)
        if name is not None:
            name = project_name(name)
        if archived is not None and type(archived) is not bool:
            raise ValueError("archived must be boolean")
        with self.store.transaction() as tx:
            old = tx.read(OWNER, identity)
            if old is None:
                raise ValueError("project does not exist")
            if expected_version is not None and expected_version != old.version:
                raise StateConflict("project changed; refresh before editing")
            value = dict(old.value)
            if name is not None:
                value["name"] = name
            if archived is not None:
                value["archived"] = archived
            return self._row(
                identity, tx.save(OWNER, identity, value, expected_version=old.version)
            )

    def bind_directory(self, identity, path):
        identity = project_id(identity)
        with self.store.transaction() as tx:
            old = tx.read(OWNER, identity)
            if old is None:
                raise ValueError("project does not exist")
            if old.value["directory"] is not None:
                if old.value["directory"] != path:
                    raise StateConflict("project directory is already fixed")
                return self._row(identity, old)
            path = str(self.directories.check_absolute(path))
            return self._row(
                identity,
                tx.save(
                    OWNER,
                    identity,
                    {**old.value, "directory": path},
                    expected_version=old.version,
                ),
            )

    def initialize_session(self, session, tx):
        identity = session.metadata.get("project_id")
        if identity is None:
            return None
        record = tx.read(OWNER, project_id(identity))
        if record is None or record.value["archived"]:
            raise ValueError("project does not exist or is archived")
        return record.value["directory"]
