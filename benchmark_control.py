"""Expiring benchmark lease shared with the recorder (no network calls)."""
import fcntl
import os
import threading
import time
from pathlib import Path

import psutil
from recorder_state import atomic_json, read_json, status_directory


def active_benchmark(base):
    lease = read_json(status_directory(base) / 'benchmark.json', {})
    try:
        return (0 <= time.time() - lease['heartbeat_at'] < 15 and
                abs(psutil.Process(lease['pid']).create_time() - lease['created_at']) < .01)
    except (KeyError, TypeError, ValueError, psutil.Error):
        return False


class BenchmarkLease:
    def __init__(self, base):
        self.directory = status_directory(base)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'benchmark.json'
        self.stop = threading.Event()
        self.thread = None
        self.lock = None
        self.identity = dict(pid=os.getpid(), created_at=psutil.Process().create_time())

    def heartbeat(self):
        atomic_json(self.path, dict(self.identity, heartbeat_at=time.time()))

    def maintain(self):
        while not self.stop.wait(2):
            try:
                self.heartbeat()
            except OSError:
                return  # Lease expires; benchmark runner must also stop.

    def __enter__(self):
        self.lock = open(str(self.directory / 'benchmark.lock'), 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.heartbeat()
        except Exception:
            self.lock.close()
            raise RuntimeError('Another benchmark is running, or its lease cannot be created')
        self.thread = threading.Thread(target=self.maintain, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        if self.thread:
            self.thread.join(3)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.lock.close()
