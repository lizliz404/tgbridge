"""One supervisor process owns polling and runtime writes for a config store."""
import json
import os

from .storage import ensure_private_dir


class AlreadyRunning(RuntimeError):
    pass


class ProcessLease:
    def __init__(self, directory):
        self.directory = directory
        self.file = None

    def acquire(self):
        # stdlib advisory locking on Linux and macOS; never unlink a lock file
        # while another process may hold its inode.
        import fcntl
        ensure_private_dir(self.directory)
        path = os.path.join(self.directory, 'bridge.lock')
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.file = os.fdopen(fd, 'r+')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            self.file = None
            raise AlreadyRunning('another bridge owns this config store') from None
        os.fchmod(fd, 0o600)
        self.file.seek(0)
        self.file.truncate()
        json.dump({'pid': os.getpid()}, self.file)
        self.file.flush()
        return self

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None
