"""One application process owns a private persistent storage directory."""
import fcntl
from pathlib import Path


class StorageLease:
    def __init__(self, directory: Path):
        self.directory = directory
        self._file = None

    def acquire(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._file = (self.directory / ".server.lock").open("a+")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file.close()
            self._file = None
            raise RuntimeError("Storage is already in use; stop the BeeSmart server or use a separate storage directory") from None

    def release(self):
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None
