"""Private state paths shared by persistence and offline administration.

These checks do not open a Store or initialize the database schema.
"""

import os
from pathlib import Path
import stat

from .errors import BridgeError


def _private_directory(path):
    directory = Path(os.path.abspath(os.fspath(path)))
    try:
        # Reject symlinks before creating anything; resolve() would hide them.
        for component in reversed((directory, *directory.parents)):
            try:
                metadata = component.lstat()
            except FileNotFoundError:
                component.mkdir(mode=0o700)
                metadata = component.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise BridgeError("unsafe_state_directory")
        metadata = directory.lstat()
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise BridgeError("unsafe_state_directory")
    except BridgeError:
        raise
    except OSError:
        raise BridgeError("state_directory_unavailable") from None
    return directory


def _private_database_file(directory):
    path = directory / "bridge.sqlite3"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
                raise BridgeError("unsafe_database_file")
        finally:
            os.close(fd)
        for suffix in ("-wal", "-shm", "-journal"):
            auxiliary = Path(str(path) + suffix)
            if auxiliary.exists() or auxiliary.is_symlink():
                metadata = auxiliary.lstat()
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                        or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
                    raise BridgeError("unsafe_database_file")
    except BridgeError:
        raise
    except OSError:
        raise BridgeError("unsafe_database_file") from None
    return path

