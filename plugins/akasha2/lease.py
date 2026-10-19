"""A checkpoint has exactly one writer, including standalone replay tools."""

import os
from pathlib import Path


class CheckpointLease:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                self._file.write(b"0")
                self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            self._file = None
            raise RuntimeError("Akasha2 checkpoint already has a writer") from exc

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None
