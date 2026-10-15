"""Cross-process exclusive writer for the unified paper database."""
import os
import sqlite3
from pathlib import Path
from ..paper_execution.store import ExecutionStore


class AtomicConnection(sqlite3.Connection):
    """Nested manager transactions join one outer input transaction."""
    depth = 0
    failed = False

    def __enter__(self):
        if self.depth == 0:
            self.failed = False
            if not self.in_transaction:
                self.execute("BEGIN IMMEDIATE")
        self.depth += 1
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.failed = self.failed or exc_type is not None
        self.depth -= 1
        if self.depth == 0:
            if self.failed:
                self.rollback()
                if exc_type is None:
                    raise RuntimeError("nested paper transaction aborted")
            else:
                self.commit()
        return False


class SingleWriterStore(ExecutionStore):
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = open(str(path)+".writer.lock", "a+b")
        try:
            self.lock.seek(0)
            if os.name == "nt":
                import msvcrt
                self.lock.write(b"0")
                self.lock.flush()
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.lock.close()
            raise RuntimeError("paper database already has a writer") from error
        try:
            super().__init__(path)
            self.db.close()
            self.db = sqlite3.connect(path, factory=AtomicConnection)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
        except Exception:
            self.lock.close()
            raise

    def close(self):
        if not self.lock.closed:
            super().close()
            self.lock.close()
