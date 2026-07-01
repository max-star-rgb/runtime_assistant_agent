from __future__ import annotations

import unittest

from openclaw_gateway_runtime.infra.metrics import GatewayMetrics, RuntimeMetrics


class MetricsTests(unittest.TestCase):
    def test_gateway_connections_gauge(self) -> None:
        m = GatewayMetrics()
        self.assertEqual(m.on_connection_open()["connections_active"], 1)
        self.assertEqual(m.on_connection_open()["connections_active"], 2)
        self.assertEqual(m.on_connection_close()["connections_active"], 1)
        self.assertEqual(m.on_connection_close()["connections_active"], 0)
        self.assertEqual(m.snapshot()["connections_total"], 2)

    def test_runtime_cancel_and_runs_started(self) -> None:
        m = RuntimeMetrics()
        self.assertEqual(m.on_run_started()["runs_started_total"], 1)
        self.assertEqual(m.on_cancel()["cancelled_total"], 1)
        self.assertEqual(m.snapshot_counts()["runs_started_total"], 1)


if __name__ == "__main__":
    unittest.main()
