"""Structured run trace: one JSON object per event, per stage timings.

Events go to the standard logger and, when a directory is given, to a
trace.jsonl file so a run can be audited or replayed after the fact.
"""

import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("invoice_ap.trace")


class Trace:
    def __init__(self, run_id: str, label: str, sink: Path | None = None):
        self.run_id, self.label, self.sink = run_id, label, sink
        self.events: list[dict] = []

    def event(self, stage: str, **detail) -> dict:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                  "run_id": self.run_id, "invoice": self.label, "stage": stage, **detail}
        self.events.append(record)
        log.info(json.dumps(record, default=str))
        if self.sink:
            self.sink.parent.mkdir(parents=True, exist_ok=True)
            with self.sink.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        return record

    @contextmanager
    def stage(self, name: str, **detail):
        started = time.perf_counter()
        try:
            yield
        finally:
            self.event(name, duration_ms=round((time.perf_counter() - started) * 1000, 1), **detail)
