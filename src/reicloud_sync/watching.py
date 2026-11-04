"""Watch local roots for hints; periodic bisync remains the source of truth."""

from dataclasses import dataclass
from pathlib import Path
from queue import Empty, SimpleQueue
import threading
import time

from watchfiles import watch

from .logging import get_cli_logger
from .sync import state_dir


@dataclass
class Handle:
    stop: threading.Event
    ready: threading.Event
    thread: threading.Thread | None = None


class WatchManager:
    def __init__(self, paths):
        self.paths = paths
        self.handles = {}
        self.events = SimpleQueue()
        self.retry_after = {}
        self.logger = get_cli_logger()

    def monitor(self, key, root, handle):
        try:
            for changes in watch(root, watch_filter=None, debounce=1000, step=200,
                                 stop_event=handle.stop, rust_timeout=500,
                                 yield_on_timeout=True, raise_interrupt=False, debug=False):
                handle.ready.set()
                if changes:
                    self.events.put(key)
        except (OSError, RuntimeError) as error:
            self.logger.warning('Local watching unavailable for %s: %s. Periodic scans continue.', root, error)
            self.retry_after[key] = time.monotonic() + 30

    def refresh(self, pairs):
        wanted = {str(state_dir(self.paths, pair)): pair.local for pair in pairs if Path(pair.local).is_dir()}
        for key, handle in list(self.handles.items()):
            if key not in wanted or not handle.thread.is_alive():
                handle.stop.set()
                handle.thread.join(timeout=1)
                del self.handles[key]
        for key, root in wanted.items():
            if key in self.handles or self.retry_after.get(key, 0) > time.monotonic():
                continue
            handle = Handle(threading.Event(), threading.Event())
            handle.thread = threading.Thread(target=self.monitor, args=(key, root, handle), daemon=True)
            self.handles[key] = handle
            handle.thread.start()

    def drain(self):
        keys = set()
        while True:
            try:
                keys.add(self.events.get_nowait())
            except Empty:
                return keys

    def close(self):
        for handle in self.handles.values():
            handle.stop.set()
        deadline = time.monotonic() + 5
        for handle in self.handles.values():
            handle.thread.join(timeout=max(0, deadline - time.monotonic()))
        self.handles.clear()
