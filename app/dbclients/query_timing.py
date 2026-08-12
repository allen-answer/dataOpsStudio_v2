from __future__ import annotations

from dataclasses import dataclass


@dataclass
class QueryTimingRecorder:
    """Mutable per-adapter recorder for the current SELECT, in milliseconds."""

    connect_ms: int = 0
    execute_ms: int = 0
    execute_first_row_ms: int = 0
    fetch_ms: int = 0
    _first_fetch_recorded: bool = False

    def reset(self) -> None:
        self.connect_ms = 0
        self.execute_ms = 0
        self.execute_first_row_ms = 0
        self.fetch_ms = 0
        self._first_fetch_recorded = False

    def record_connect(self, elapsed_ms: int) -> None:
        self.connect_ms = max(0, elapsed_ms)

    def record_execute(self, elapsed_ms: int) -> None:
        self.execute_ms = max(0, elapsed_ms)

    def record_fetch(self, elapsed_ms: int) -> None:
        elapsed = max(0, elapsed_ms)
        if not self._first_fetch_recorded:
            self.execute_first_row_ms = self.execute_ms + elapsed
            self._first_fetch_recorded = True
            return
        self.fetch_ms += elapsed

    def snapshot(self) -> dict[str, int]:
        return {
            "connect_ms": self.connect_ms,
            "execute_first_row_ms": self.execute_first_row_ms,
            "fetch_ms": self.fetch_ms,
        }


__all__ = ["QueryTimingRecorder"]
