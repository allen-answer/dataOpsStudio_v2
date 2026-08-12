from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class QueryTimingRecorder:
    """Mutable per-adapter recorder for the current SELECT, in milliseconds."""

    connect_ms: int = 0
    execute_ms: int = 0
    execute_first_row_ms: int = 0
    fetch_ms: int = 0
    _first_fetch_recorded: bool = False
    connect_started_at: str | None = None
    connected_at: str | None = None
    execute_started_at: str | None = None
    first_row_at: str | None = None

    def reset(self) -> None:
        self.connect_ms = 0
        self.execute_ms = 0
        self.execute_first_row_ms = 0
        self.fetch_ms = 0
        self._first_fetch_recorded = False
        self.connect_started_at = None
        self.connected_at = None
        self.execute_started_at = None
        self.first_row_at = None

    def mark_connect_started(self) -> None:
        self.connect_started_at = _utc_now()

    def record_connect(self, elapsed_ms: int) -> None:
        self.connect_ms = max(0, elapsed_ms)
        self.connected_at = _utc_now()

    def mark_execute_started(self) -> None:
        self.execute_started_at = _utc_now()

    def record_execute(self, elapsed_ms: int) -> None:
        self.execute_ms = max(0, elapsed_ms)

    def record_fetch(self, elapsed_ms: int, *, row_count: int) -> None:
        elapsed = max(0, elapsed_ms)
        if row_count > 0 and not self._first_fetch_recorded:
            self.execute_first_row_ms = self.execute_ms + elapsed
            self._first_fetch_recorded = True
            self.first_row_at = _utc_now()
            return
        self.fetch_ms += elapsed

    def snapshot(self) -> dict[str, int]:
        return {
            "connect_ms": self.connect_ms,
            "execute_first_row_ms": self.execute_first_row_ms,
            "fetch_ms": self.fetch_ms,
        }

    def execution_snapshot(self) -> dict[str, int | str | None]:
        return {
            **self.snapshot(),
            "connect_started_at": self.connect_started_at,
            "connected_at": self.connected_at,
            "execute_started_at": self.execute_started_at,
            "first_row_at": self.first_row_at,
        }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["QueryTimingRecorder"]
