"""A cross-platform "one run at a time" lock (fcntl on macOS/Linux, msvcrt on Windows)."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path


class Busy(Exception):
    pass


def _try_lock(f) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(f) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def hold(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        if not _try_lock(f):
            raise Busy(str(path))
        try:
            yield
        finally:
            _unlock(f)


def is_held(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open("a+") as f:
        if _try_lock(f):
            _unlock(f)
            return False
        return True
