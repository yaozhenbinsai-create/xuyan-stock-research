"""Bounded UTF-8 stdout/stderr for the single Windows-managed service."""
import io
from pathlib import Path
import threading
from foundation.storage_policy import POLICY


class BoundedLog(io.TextIOBase):
    def __init__(self, path, *, max_bytes=None, backup_count=None):
        self.path=Path(path)
        self.max_bytes=max_bytes or POLICY['log_max_bytes']
        self.backup_count=backup_count if backup_count is not None else POLICY['log_backups']
        self._lock=threading.RLock()
        self.stream=self.path.open('a',encoding='utf-8',buffering=1)

    @property
    def encoding(self):
        return "utf-8"

    def write(self, value):
        size = len(value)
        value = value[:16384]
        with self._lock:
            # A stdout/stderr sink must not register a logging.Handler: server
            # dictConfig closes registered handlers during its initialization.
            if self.stream.tell()+len(value.encode('utf-8'))>self.max_bytes:
                self.stream.close()
                for index in range(self.backup_count,0,-1):
                    source=self.path if index==1 else self.path.with_name(self.path.name+'.'+str(index-1))
                    destination=self.path.with_name(self.path.name+'.'+str(index))
                    if source.exists():
                        destination.unlink(missing_ok=True)
                        source.replace(destination)
                self.stream=self.path.open('a',encoding='utf-8',buffering=1)
            self.stream.write(value)
            self.stream.flush()
        return size

    def flush(self):
        stream=getattr(self,'stream',None)
        if stream is not None and not stream.closed:
            with self._lock:stream.flush()

    def close(self):
        stream=getattr(self,'stream',None)
        if stream is not None and not stream.closed:
            with self._lock:stream.close()
        super().close()
