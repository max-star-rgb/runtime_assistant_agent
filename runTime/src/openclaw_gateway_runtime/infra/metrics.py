from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GatewayMetrics:
    """
    Gateway process counters/gauges.

    Single-threaded asyncio per process: no threading.Lock (mixing threading.Lock with asyncio
    on Windows has caused event-loop stalls in practice).
    """

    connections_active: int = 0
    connections_total: int = 0

    def on_connection_open(self) -> dict[str, int]:
        self.connections_active += 1
        self.connections_total += 1
        return self.snapshot()

    def on_connection_close(self) -> dict[str, int]:
        self.connections_active = max(0, self.connections_active - 1)
        return self.snapshot()

    def snapshot(self) -> dict[str, int]:
        return {
            "connections_active": self.connections_active,
            "connections_total": self.connections_total,
        }


@dataclass
class RuntimeMetrics:
    """
    Runtime process counters (runs started / explicit+implicit cancels).

    Single-threaded asyncio per process: no threading.Lock (see GatewayMetrics).
    """

    runs_started_total: int = 0
    cancelled_total: int = 0

    def on_run_started(self) -> dict[str, int]:
        self.runs_started_total += 1
        return self.snapshot_counts()

    def on_cancel(self) -> dict[str, int]:
        self.cancelled_total += 1
        return self.snapshot_counts()

    def snapshot_counts(self) -> dict[str, int]:
        return {
            "runs_started_total": self.runs_started_total,
            "cancelled_total": self.cancelled_total,
        }


# Process-wide singletons (one per process: gateway vs runtime).
gateway_metrics = GatewayMetrics()
runtime_metrics = RuntimeMetrics()
