"""Keep regression tests away from actual repository runtime databases."""

import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def no_repository_database_access(monkeypatch):
    connect = sqlite3.connect

    def isolated(database, *args, **kwargs):
        address = str(database)
        if address != ":memory:" and "mode=memory" not in address:
            path = Path(
                unquote(urlsplit(address).path)
                if address.startswith("file:")
                else address
            ).resolve()
            if path.is_relative_to(ROOT):
                raise AssertionError(
                    "tests must use an isolated workspace, not " + str(path)
                )
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", isolated)
