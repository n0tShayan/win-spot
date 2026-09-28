"""Background job runner.

Each Worker is a single thread that runs jobs in submission order, so API calls
never block the UI and never race each other. Results are posted to a shared
queue and their callbacks run on the UI thread, which owns all app state.
"""

import queue
import threading


class Worker:
    def __init__(self, name: str, results: queue.SimpleQueue):
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._results = results
        threading.Thread(target=self._run, name=name, daemon=True).start()

    def submit(self, fn, on_done=None, on_error=None) -> None:
        self._jobs.put((fn, on_done, on_error))

    def _run(self) -> None:
        while True:
            fn, on_done, on_error = self._jobs.get()
            try:
                value = fn()
            except Exception as e:  # handed to the UI thread, never lost
                self._results.put((on_error, e))
            else:
                self._results.put((on_done, value))
