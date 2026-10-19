"""Compose Project defaults and Session directory initialization atomically."""

from dataclasses import dataclass

from plugins.projects.service import PROJECTS, Projects
from plugins.working_directory.service import WORKING_DIRECTORY, WorkingDirectories
from session.owner_store import OwnerStore


@dataclass
class WorkspaceServices:
    projects: Projects
    directories: WorkingDirectories
    admission: object = None


def build_workspace_services(sessions):
    store = OwnerStore(sessions)
    directories = WorkingDirectories(store, sessions.workspace)
    projects = Projects(store, directories)

    def initialize(session, tx):
        default = projects.initialize_session(session, tx)
        directories.initialize(session, tx, default)

    sessions.register_initializer("workspace", initialize)
    return WorkspaceServices(projects, directories)


def publish_workspace_services(registry, services):
    registry.publish(
        "workspace",
        {PROJECTS: services.projects, WORKING_DIRECTORY: services.directories},
    )
