#!/usr/bin/env python3
"""Unit tests for the pure/logic-heavy pieces of resource_dashboard.py.

Deliberately avoids touching real /proc files, real processes, or the
network — those are exercised by manual/integration testing instead.
Run with: python3 -m unittest discover -s tests
"""
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import resource_dashboard as rd  # noqa: E402


class ClampTests(unittest.TestCase):
    def test_within_range(self):
        self.assertEqual(rd.clamp(50), 50)

    def test_below_low(self):
        self.assertEqual(rd.clamp(-10), 0)

    def test_above_high(self):
        self.assertEqual(rd.clamp(150), 100)

    def test_custom_bounds(self):
        self.assertEqual(rd.clamp(5, low=10, high=20), 10)


class HumanBytesTests(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(rd.human_bytes(500), "500.0 B")

    def test_kb(self):
        self.assertEqual(rd.human_bytes(2048), "2.0 KB")

    def test_gb(self):
        self.assertEqual(rd.human_bytes(3 * 1024**3), "3.0 GB")

    def test_top_unit_does_not_overflow(self):
        # Should clamp at PB rather than raising an index error.
        huge = 5 * 1024**6
        self.assertTrue(rd.human_bytes(huge).endswith("PB"))


class SanitizeForJsonTests(unittest.TestCase):
    def test_nan_becomes_none(self):
        self.assertIsNone(rd.sanitize_for_json(float("nan")))

    def test_inf_becomes_none(self):
        self.assertIsNone(rd.sanitize_for_json(float("inf")))
        self.assertIsNone(rd.sanitize_for_json(float("-inf")))

    def test_normal_float_untouched(self):
        self.assertEqual(rd.sanitize_for_json(3.5), 3.5)

    def test_nested_structures(self):
        result = rd.sanitize_for_json({"a": [1.0, float("nan")], "b": {"c": float("inf")}})
        self.assertEqual(result, {"a": [1.0, None], "b": {"c": None}})


class KillProcessGuardTests(unittest.TestCase):
    def test_refuses_pid_zero(self):
        result = rd.kill_process(0, "TERM")
        self.assertFalse(result["success"])

    def test_refuses_pid_one(self):
        result = rd.kill_process(1, "TERM")
        self.assertFalse(result["success"])

    def test_refuses_self(self):
        result = rd.kill_process(os.getpid(), "TERM")
        self.assertFalse(result["success"])
        self.assertIn("dashboard server itself", result["message"])

    def test_rejects_unsupported_signal(self):
        result = rd.kill_process(os.getpid() + 1, "BOGUS")
        self.assertFalse(result["success"])
        self.assertIn("Unsupported signal", result["message"])

    def test_no_such_process(self):
        # Spawn and reap a process so its PID is guaranteed to be free.
        proc = subprocess.Popen(["true"])
        proc.wait()
        result = rd.kill_process(proc.pid, "TERM")
        self.assertFalse(result["success"])
        self.assertIn("No such process", result["message"])

    def test_successfully_signals_real_process(self):
        proc = subprocess.Popen(["sleep", "30"])
        try:
            result = rd.kill_process(proc.pid, "TERM")
            self.assertTrue(result["success"])
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class GenerateCsvTests(unittest.TestCase):
    def setUp(self):
        with rd.STATE_LOCK:
            self._saved_rows = list(rd.STATE.csv_rows)
            rd.STATE.csv_rows = [
                {"timestamp": 100.0, "cpu_percent": 1.0},
                {"timestamp": 200.0, "cpu_percent": 2.0},
                {"timestamp": 300.0, "cpu_percent": 3.0},
            ]

    def tearDown(self):
        with rd.STATE_LOCK:
            rd.STATE.csv_rows = self._saved_rows

    def test_no_filter_returns_all_rows(self):
        csv_text = rd.generate_csv()
        self.assertEqual(csv_text.count("\n"), 4)  # header + 3 rows

    def test_since_filter(self):
        csv_text = rd.generate_csv(since=200.0)
        self.assertNotIn("100.0", csv_text)
        self.assertIn("200.0", csv_text)
        self.assertIn("300.0", csv_text)

    def test_empty_state_message(self):
        with rd.STATE_LOCK:
            rd.STATE.csv_rows = []
        self.assertEqual(rd.generate_csv(), "No data collected yet.\n")


class EvaluateAlertsTests(unittest.TestCase):
    def _base_data(self, **overrides):
        data = {
            "cpu_percent": 10.0,
            "iowait_percent": 1.0,
            "memory": {"percent": 20.0},
            "gpu": [],
        }
        data.update(overrides)
        return data

    def test_no_alerts_when_under_threshold(self):
        alerts = rd.evaluate_alerts(self._base_data(), config=rd.DEFAULT_ALERT_THRESHOLDS)
        self.assertEqual(alerts, [])

    def test_cpu_alert_fires_over_threshold(self):
        data = self._base_data(cpu_percent=99.0)
        alerts = rd.evaluate_alerts(data, config=rd.DEFAULT_ALERT_THRESHOLDS)
        self.assertTrue(any("CPU" in a for a in alerts))

    def test_boundary_is_not_an_alert(self):
        cfg = rd.DEFAULT_ALERT_THRESHOLDS
        data = self._base_data(cpu_percent=cfg["cpu_percent"])  # exactly at threshold
        alerts = rd.evaluate_alerts(data, config=cfg)
        self.assertEqual(alerts, [])

    def test_gpu_temperature_alert(self):
        data = self._base_data(gpu=[{"index": "0", "temperature": "90"}])
        alerts = rd.evaluate_alerts(data, config=rd.DEFAULT_ALERT_THRESHOLDS)
        self.assertTrue(any("GPU#0" in a for a in alerts))

    def test_non_numeric_gpu_temp_is_ignored(self):
        data = self._base_data(gpu=[{"index": "0", "temperature": "N/A"}])
        alerts = rd.evaluate_alerts(data, config=rd.DEFAULT_ALERT_THRESHOLDS)
        self.assertEqual(alerts, [])


class RenderPrometheusTests(unittest.TestCase):
    def test_contains_core_metrics(self):
        data = {
            "cpu_percent": 12.3,
            "iowait_percent": 0.5,
            "memory": {"percent": 40.0},
            "disk": {"percent": 55.0},
            "disk_io": {"read_rate": 100.0, "write_rate": 200.0},
            "network": {"rx_rate": 300.0, "tx_rate": 400.0},
            "zombies": 0,
            "gpu": [],
        }
        text = rd.render_prometheus(data)
        self.assertIn("resource_dashboard_cpu_percent 12.3", text)
        self.assertIn("resource_dashboard_mem_percent 40.0", text)
        self.assertIn("# TYPE resource_dashboard_disk_percent gauge", text)

    def test_gpu_metrics_included_when_present(self):
        data = {
            "cpu_percent": 0, "iowait_percent": 0, "memory": {"percent": 0},
            "disk": {"percent": 0}, "disk_io": {"read_rate": 0, "write_rate": 0},
            "network": {"rx_rate": 0, "tx_rate": 0}, "zombies": 0,
            "gpu": [{"index": "0", "utilization": "55", "temperature": "60", "memory_used": "1000"}],
        }
        text = rd.render_prometheus(data)
        self.assertIn('resource_dashboard_gpu_utilization_percent{gpu="0"} 55', text)


class HistoryDbTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_path = rd.HISTORY_DB_PATH
        rd.HISTORY_DB_PATH = Path(self._tmpdir.name) / "history.db"
        rd.init_history_db()

    def tearDown(self):
        rd.HISTORY_DB_PATH = self._orig_path
        self._tmpdir.cleanup()

    def _insert(self, **row):
        import sqlite3
        conn = sqlite3.connect(rd.HISTORY_DB_PATH)
        cols = list(row.keys())
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(f"INSERT INTO metrics ({', '.join(cols)}) VALUES ({placeholders})", list(row.values()))
        conn.commit()
        conn.close()

    def test_query_filters_by_since(self):
        self._insert(timestamp=100.0, cpu_percent=1.0)
        self._insert(timestamp=200.0, cpu_percent=2.0)
        rows = rd.query_history(since=150.0, until=None, limit=100)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timestamp"], 200.0)

    def test_query_respects_limit_and_order(self):
        for i in range(5):
            self._insert(timestamp=float(i), cpu_percent=float(i))
        rows = rd.query_history(since=None, until=None, limit=3)
        self.assertEqual(len(rows), 3)
        # Should be the 3 most recent, returned in ascending time order.
        self.assertEqual([r["timestamp"] for r in rows], [2.0, 3.0, 4.0])


if __name__ == "__main__":
    unittest.main()
