"""
Unit tests for the Memory Spot Price Monitor's delta/alert logic.

Run with:  python -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory_monitor import (  # noqa: E402
    compute_calendar_signals,
    evaluate_memory_alerts,
    render_digest,
)

THRESHOLDS = {
    "weekly_drop_pct": -2.0,
    "rollover_monthly_min_pct": 2.0,
    "stale_after_days": 7,
}


class TestCalendarSignals(unittest.TestCase):
    def test_daily_series_deltas(self):
        # ~5 weeks of daily data rising 1% every ~week
        obs = [
            ("2026-07-07", 48.0), ("2026-07-14", 48.5), ("2026-07-21", 49.0),
            ("2026-07-28", 50.0), ("2026-08-04", 51.0), ("2026-08-11", 51.6),
        ]
        sig = compute_calendar_signals(obs)
        self.assertEqual(sig["level"], 51.6)
        self.assertEqual(sig["last_date"], "2026-08-11")
        self.assertAlmostEqual(sig["d1w"], 1.18, places=2)   # base 08-04 (51.0)
        # 1-month base: closest obs at-or-before 07-12 is 07-07 (48.0),
        # 35 days old — inside the 2x-window honesty bound, so it's used.
        self.assertAlmostEqual(sig["d1m"], 7.5, places=1)
        self.assertIsNone(sig["d3m"])                        # no 3-month base

    def test_sparse_monthly_series_does_not_fake_weekly_delta(self):
        # The blogger's actual cadence: month-end points only. A "Δ1w" from
        # a month-old base would be a lie — must be None.
        obs = [("2026-03-31", 39.45), ("2026-06-30", 48.0), ("2026-07-31", 50.97)]
        sig = compute_calendar_signals(obs)
        self.assertIsNone(sig["d1w"])
        self.assertAlmostEqual(sig["d1m"], 6.19, places=2)   # 48.0 -> 50.97
        # 3-month base: Mar 31 print is 122 days back — older than the 91d
        # target but inside the 2x bound (182d), so the delta is reported.
        self.assertAlmostEqual(sig["d3m"], 29.2, places=1)

    def test_base_honesty_bound(self):
        # Base observation just inside the 2x window is used...
        obs = [("2026-08-01", 50.0), ("2026-08-11", 51.0)]
        self.assertAlmostEqual(compute_calendar_signals(obs)["d1w"], 2.0, places=1)
        # ...but one older than 2x the window is rejected
        obs = [("2026-07-25", 50.0), ("2026-08-11", 51.0)]
        self.assertIsNone(compute_calendar_signals(obs)["d1w"])

    def test_rollover_flag(self):
        # Up on the month, down on the week => rolling over
        obs = [
            ("2026-07-11", 48.0), ("2026-07-18", 49.5), ("2026-07-25", 51.0),
            ("2026-08-01", 52.0), ("2026-08-04", 52.5), ("2026-08-11", 51.8),
        ]
        sig = compute_calendar_signals(obs)
        self.assertLess(sig["d1w"], 0)
        self.assertGreater(sig["d1m"], 0)
        self.assertTrue(sig["rolling_over"])

        # Down on both week and month is a downtrend, not a fresh rollover
        obs = [("2026-07-11", 55.0), ("2026-07-25", 53.0),
               ("2026-08-04", 52.0), ("2026-08-11", 51.0)]
        self.assertFalse(compute_calendar_signals(obs)["rolling_over"])

    def test_acceleration_flag(self):
        # Week-over-week gains speeding up: +1% then +3%
        obs = [
            ("2026-07-21", 48.0), ("2026-07-28", 48.5),
            ("2026-08-04", 49.0), ("2026-08-11", 50.5),
        ]
        sig = compute_calendar_signals(obs)
        self.assertGreater(sig["d1w"], sig["d1w_prev"])
        self.assertTrue(sig["accelerating"])

    def test_empty_series(self):
        sig = compute_calendar_signals([])
        self.assertIsNone(sig["level"])
        self.assertFalse(sig["rolling_over"])


class TestMemoryAlerts(unittest.TestCase):
    def _sig(self, d1w, d1m):
        return {"d1w": d1w, "d1m": d1m, "level": 50.0, "last_date": "2026-08-11"}

    def test_no_alerts_when_rising(self):
        alerts = evaluate_memory_alerts(
            {"DDR5_16G": self._sig(1.2, 7.5)}, THRESHOLDS)
        self.assertEqual(alerts, [])

    def test_weekly_drop_warn(self):
        alerts = evaluate_memory_alerts(
            {"DDR5_16G": self._sig(-2.5, 3.0)}, THRESHOLDS)
        self.assertEqual([a["code"] for a in alerts],
                         ["memory_weekly_drop:DDR5_16G"])
        self.assertEqual(alerts[0]["level"], "WARN")

    def test_fresh_rollover_warn(self):
        # Small weekly dip (-0.8%, above the -2% drop bar) while still up
        # big on the month => rollover WARN
        alerts = evaluate_memory_alerts(
            {"NAND_512G": self._sig(-0.8, 6.0)}, THRESHOLDS)
        self.assertEqual([a["code"] for a in alerts],
                         ["memory_rollover:NAND_512G"])

    def test_small_dip_in_flat_market_is_noise(self):
        # -0.8% weekly with a flat month: neither drop nor rollover
        alerts = evaluate_memory_alerts(
            {"DDR5_16G": self._sig(-0.8, 0.5)}, THRESHOLDS)
        self.assertEqual(alerts, [])

    def test_missing_data_is_silent(self):
        alerts = evaluate_memory_alerts(
            {"DXI": self._sig(None, None)}, THRESHOLDS)
        self.assertEqual(alerts, [])


class TestDigest(unittest.TestCase):
    def test_digest_renders_alerts_and_rows(self):
        snapshot = {
            "alerts": [{"level": "WARN", "code": "memory_weekly_drop:DDR5_16G",
                        "message": "DDR5 spot fell -2.5% in a week"}],
            "series": [
                {"series_id": "DXI", "name": "DXI Index", "unit": "index",
                 "level": 965147.7, "d1w": 1.1, "d1m": 4.2, "d3m": 12.0,
                 "accelerating": False, "rolling_over": False,
                 "stale": False, "last_date": "2026-08-11"},
                {"series_id": "DDR5_16G", "name": "DDR5 16Gb 4800/5600",
                 "unit": "usd", "level": 51.6, "d1w": -2.5, "d1m": 3.0,
                 "d3m": None, "accelerating": False, "rolling_over": True,
                 "stale": True, "last_date": "2026-08-01"},
            ],
            "notes": [], "source": "https://www.dramexchange.com/",
        }
        text = render_digest(snapshot)
        self.assertIn("WARN", text)
        self.assertIn("965,148", text)
        self.assertIn("$51.60", text)
        self.assertIn("ROLLING OVER", text)
        self.assertIn("stale as of 2026-08-01", text)

    def test_digest_empty_snapshot(self):
        text = render_digest({"alerts": [], "series": [], "notes": []})
        self.assertIn("No alerts", text)


if __name__ == "__main__":
    unittest.main()
