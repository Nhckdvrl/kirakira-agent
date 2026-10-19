"""Versioned owner records in the same transaction domain as Session creation."""

import json
from contextlib import contextmanager
from dataclasses import dataclass


class StateConflict(ValueError):
    pass


@dataclass(frozen=True)
class Record:
    version: int
    value: dict


def ensure_owner_records(db):
    db.execute("""CREATE TABLE IF NOT EXISTS owner_records (
        owner TEXT NOT NULL, key TEXT NOT NULL, version INTEGER NOT NULL,
        value_json TEXT NOT NULL, PRIMARY KEY(owner,key))""")


class OwnerTransaction:
    def __init__(self, db):
        self.db = db

    def read(self, owner, key):
        row = self.db.execute(
            "SELECT version,value_json FROM owner_records WHERE owner=? AND key=?",
            (owner, key),
        ).fetchone()
        return None if row is None else Record(row[0], json.loads(row[1]))

    def save(self, owner, key, value, *, expected_version):
        current = self.read(owner, key)
        if (None if current is None else current.version) != expected_version:
            raise StateConflict(f"state changed: {owner}/{key}")
        version = 0 if current is None else current.version + 1
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        self.db.execute(
            "INSERT INTO owner_records VALUES (?,?,?,?) "
            "ON CONFLICT(owner,key) DO UPDATE SET version=excluded.version,value_json=excluded.value_json",
            (owner, key, version, payload),
        )
        return Record(version, json.loads(payload))

    def scan(self, owner, prefix=""):
        rows = self.db.execute(
            "SELECT key,version,value_json FROM owner_records WHERE owner=? "
            "AND key>=? AND key<? ORDER BY key",
            (owner, prefix, prefix + "\uffff"),
        )
        return [(row[0], Record(row[1], json.loads(row[2]))) for row in rows]


class OwnerStore:
    def __init__(self, sessions):
        self.sessions = sessions

    @contextmanager
    def transaction(self):
        with self.sessions._index_lock:
            db = self.sessions._index
            db.execute("BEGIN IMMEDIATE")
            try:
                yield OwnerTransaction(db)
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def read(self, owner, key):
        with self.sessions._index_lock:
            return OwnerTransaction(self.sessions._index).read(owner, key)

    def scan(self, owner, prefix=""):
        with self.sessions._index_lock:
            return OwnerTransaction(self.sessions._index).scan(owner, prefix)
