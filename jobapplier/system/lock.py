import fcntl
import os
from contextlib import contextmanager


@contextmanager
def locked(path: str):
    """Yields True holding an exclusive lock, False if another process has it."""
    with open(path, "a+") as f:  # append mode: a failed attempt must not wipe the holder's PID
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        f.truncate(0)
        f.write(str(os.getpid()))
        f.flush()
        yield True  # closing the file releases the lock
