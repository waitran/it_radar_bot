"""User-storage lifecycle lock; entirely separate from the bot's instance lock."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import sqlite3


def protect_catalog(path, catalog_path):
    path, catalog = Path(path).resolve(), Path(catalog_path).resolve()
    protected = [catalog] + [Path(str(catalog) + suffix) for suffix in ('-wal', '-shm', '-journal', '.lock')]
    for candidate in (path, Path(str(path) + '.service.lock')):
        for other in protected:
            if candidate == other or (candidate.exists() and other.exists() and os.path.samefile(candidate, other)):
                raise ValueError('Mini App user storage must be separate from catalog storage')
    return path


@contextmanager
def storage_lock(path, *, exclusive=False):
    descriptor = os.open(str(path) + '.service.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Invalid user storage lock')
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise sqlite3.OperationalError('Stop the API before restoring user storage') from None
        yield
    finally:
        os.close(descriptor)
