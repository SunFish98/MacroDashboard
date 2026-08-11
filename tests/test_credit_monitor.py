"""
Unit tests for the Credit Risk Monitor's delta/alert logic.

Run with:  python -m pytest tests/ -v
(or: python -m unittest discover tests)
"""

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from credit_monitor import (  # noqa: E402
    CreditHistoryStore,
    compute_signals,
    evaluate_alerts,
    is_stale,
    render_digest,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def make_series(values: list[float], end: str = "2026-08-10") -> list[tuple[str, float]]:
    """Build ascending (date, value) pairs ending at `end`, one per weekday."""
    end_date = date.fromisoformat(end)
    out = []
    d = end_date
    for v in reversed(values):
        while d.weekday() >= 5:  # skip weekends, like real market data
            d -= timedelta(days=1)
        out.append((d.isoformat(), v))
        d -= timedelta(days=1)
    out.reverse()
    return out


THRESHOLDS = {
    "window_trading_days": 5,
    "spread_widening_bp": {"BAMLC0A4CBBB": 20.0, "BAMLH0A0HYM2": 50.0},
    "cds_level_bp": {"NVDA": 100.0, "ORCL": 250.0},
    "cds_1d_widening_bp": 15.0,
    "divergence_equity_flat_pct": 0.0,
    "stale_after_days": 5,
}

# Flat BBB OAS at 1.20% (120bp) for 20 days, then +0.05%/day for 5 days
# => +25bp over the 5-day window: trips the 20bp BBB WARN.
BBB_WIDENING = [1.20] * 20 + [1.25, 1.30, 1.35, 1.40, 1.45]
BBB_FLAT = [1.20] * 25
HY_WIDENING = [3.00] * 20 + [3.15, 3.30, 3.45, 3.55, 3.65]  # +65bp in 5 days

# QQQ flat/up over the same window (the divergence case)
QQQ_FLAT = [600.0] * 24 + [601.0]
# QQQ selling off alongside credit (NOT a divergence)
QQQ_DOWN = [600.0] * 20 + [590.0, 580.0, 572.0, 565.0, 558.0]


# ---------------------------------------------------------------------------
# compute_signals
# ---------------------------------------------------------------------------

class TestComputeSignals(unittest.TestCase):
    def test_deltas_in_bp(self):
        sig = compute_signals(make_series(BBB_WIDENING), unit="bp")
        self.assertEqual(sig["level"], 145.0)          # 1.45% -> 145bp
        self.assertEqual(sig["d1"], 5.0)               # 1.40 -> 1.45
        self.assertEqual(sig["d1w"], 25.0)             # 1.20 -> 1.45
        self.assertEqual(sig["d1m"], 25.0)             # flat before the move
        self.assertEqual(sig["last_date"], "2026-08-10")

    def test_deltas_in_pct(self):
        sig = compute_signals(make_series(QQQ_DOWN), unit="pct")
        self.assertEqual(sig["level"], 558.0)
        self.assertAlmostEqual(sig["d1w"], -7.0, places=1)  # 600 -> 558

    def test_acceleration_flag(self):
        # Prior week flat (d1w_prev = 0), this week +25bp => accelerating
        sig = compute_signals(make_series(BBB_WIDENING), unit="bp")
        self.assertEqual(sig["d1w_prev"], 0.0)
        self.assertTrue(sig["accelerating"])

        # Steady widening at the same pace is NOT accelerating
        steady = [1.00 + 0.01 * i for i in range(25)]
        sig = compute_signals(make_series(steady), unit="bp")
        self.assertEqual(sig["d1w"], sig["d1w_prev"])
        self.assertFalse(sig["accelerating"])

        # Tightening is never "accelerating" (flag tracks widening risk only)
        tightening = [2.00] * 20 + [1.90, 1.80, 1.70, 1.60, 1.50]
        sig = compute_signals(make_series(tightening), unit="bp")
        self.assertFalse(sig["accelerating"])

    def test_acceleration_direction_for_etf_prices(self):
        # For bond ETF prices, FALLING is the risk direction: an accelerating
        # sell-off flags, an accelerating rally does not.
        selloff = [110.0] * 20 + [109.0, 108.0, 107.0, 105.5, 104.0]
        sig = compute_signals(make_series(selloff), unit="pct", risk_direction="down")
        self.assertTrue(sig["accelerating"])
        rally = [110.0] * 20 + [111.0, 112.0, 113.0, 114.5, 116.0]
        sig = compute_signals(make_series(rally), unit="pct", risk_direction="down")
        self.assertFalse(sig["accelerating"])

    def test_insufficient_history(self):
        sig = compute_signals(make_series([1.20, 1.22]), unit="bp")
        self.assertEqual(sig["level"], 122.0)
        self.assertEqual(sig["d1"], 2.0)
        self.assertIsNone(sig["d1w"])
        self.assertIsNone(sig["d1m"])
        self.assertFalse(sig["accelerating"])

    def test_empty_and_none_values(self):
        self.assertIsNone(compute_signals([], unit="bp")["level"])
        sig = compute_signals([("2026-08-07", None), ("2026-08-10", 1.5)], unit="bp")
        self.assertEqual(sig["level"], 150.0)
        self.assertIsNone(sig["d1"])


class TestStaleness(unittest.TestCase):
    def test_fresh_within_publication_lag(self):
        # Friday's FRED print viewed on Monday (T+1 + weekend) is fresh
        self.assertFalse(is_stale("2026-08-07", "2026-08-10", 5))

    def test_stale_beyond_lag(self):
        self.assertTrue(is_stale("2026-08-01", "2026-08-10", 5))

    def test_missing_date_is_stale(self):
        self.assertTrue(is_stale(None, "2026-08-10", 5))
        self.assertTrue(is_stale("not-a-date", "2026-08-10", 5))


# ---------------------------------------------------------------------------
# evaluate_alerts
# ---------------------------------------------------------------------------

def spreads(bbb: list[float], hy: list[float]) -> dict:
    return {
        "BAMLC0A4CBBB": compute_signals(make_series(bbb), unit="bp"),
        "BAMLH0A0HYM2": compute_signals(make_series(hy), unit="bp"),
    }


class TestAlerts(unittest.TestCase):
    def test_no_alerts_when_flat(self):
        alerts = evaluate_alerts(
            spreads(BBB_FLAT, [3.00] * 25), [],
            compute_signals(make_series(QQQ_FLAT), unit="pct"), THRESHOLDS,
        )
        self.assertEqual(alerts, [])

    def test_bbb_widening_warn(self):
        alerts = evaluate_alerts(spreads(BBB_WIDENING, [3.00] * 25), [], None, THRESHOLDS)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["level"], "WARN")
        self.assertIn("spread_widening:BAMLC0A4CBBB", alerts[0]["code"])
        self.assertIn("+25bp", alerts[0]["message"])

    def test_hy_widening_warn_threshold_edge(self):
        # +45bp in 5 days is under the 50bp HY threshold -> no alert
        hy_under = [3.00] * 20 + [3.10, 3.20, 3.30, 3.38, 3.45]
        alerts = evaluate_alerts(spreads(BBB_FLAT, hy_under), [], None, THRESHOLDS)
        self.assertEqual(alerts, [])
        # +65bp trips it
        alerts = evaluate_alerts(spreads(BBB_FLAT, HY_WIDENING), [], None, THRESHOLDS)
        self.assertEqual([a["code"] for a in alerts], ["spread_widening:BAMLH0A0HYM2"])

    def test_cds_level_warn(self):
        cds = [{"entity": "NVDA", "spread_bp": 112.0, "date": "2026-08-08",
                "source_url": "https://example.com/a"}]
        alerts = evaluate_alerts(spreads(BBB_FLAT, [3.00] * 25), cds, None, THRESHOLDS)
        self.assertEqual([a["code"] for a in alerts], ["cds_level:NVDA"])

        # Non-thresholded entity at a high level does not fire a level alert
        cds = [{"entity": "META", "spread_bp": 300.0, "date": "2026-08-08",
                "source_url": "https://example.com/b"}]
        alerts = evaluate_alerts(spreads(BBB_FLAT, [3.00] * 25), cds, None, THRESHOLDS)
        self.assertEqual(alerts, [])

    def test_cds_short_window_widening(self):
        cds = [
            {"entity": "META", "spread_bp": 60.0, "date": "2026-08-07",
             "source_url": "https://example.com/a"},
            {"entity": "META", "spread_bp": 80.0, "date": "2026-08-08",
             "source_url": "https://example.com/b"},
        ]
        alerts = evaluate_alerts(spreads(BBB_FLAT, [3.00] * 25), cds, None, THRESHOLDS)
        self.assertEqual([a["code"] for a in alerts], ["cds_widening:META"])

        # Same widening across a 3-week gap is not a "1-day" signal
        cds[0]["date"] = "2026-07-15"
        alerts = evaluate_alerts(spreads(BBB_FLAT, [3.00] * 25), cds, None, THRESHOLDS)
        self.assertEqual(alerts, [])

    def test_divergence_escalates_to_alert(self):
        """Credit WARN + flat/up QQQ => the 2007-style divergence ALERT."""
        alerts = evaluate_alerts(
            spreads(BBB_WIDENING, [3.00] * 25), [],
            compute_signals(make_series(QQQ_FLAT), unit="pct"), THRESHOLDS,
        )
        levels = {a["code"]: a["level"] for a in alerts}
        self.assertEqual(levels["divergence_2007_style"], "ALERT")
        self.assertEqual(levels["spread_widening:BAMLC0A4CBBB"], "WARN")
        # ALERT is sorted first for the digest
        self.assertEqual(alerts[0]["code"], "divergence_2007_style")

    def test_no_divergence_when_equities_sell_off_too(self):
        alerts = evaluate_alerts(
            spreads(BBB_WIDENING, [3.00] * 25), [],
            compute_signals(make_series(QQQ_DOWN), unit="pct"), THRESHOLDS,
        )
        self.assertEqual([a["code"] for a in alerts], ["spread_widening:BAMLC0A4CBBB"])

    def test_no_divergence_without_spread_warn(self):
        alerts = evaluate_alerts(
            spreads(BBB_FLAT, [3.00] * 25), [],
            compute_signals(make_series(QQQ_FLAT), unit="pct"), THRESHOLDS,
        )
        self.assertEqual(alerts, [])


# ---------------------------------------------------------------------------
# Store + digest
# ---------------------------------------------------------------------------

class TestStore(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.path = Path(self.tmpdir) / "credit_history.json"
        self.store = CreditHistoryStore(self.path)

    def test_append_only(self):
        added = self.store.append_observations("BAMLH0A0HYM2", {"2026-08-07": 3.02})
        self.assertEqual(added, 1)
        # A later "revision" for the same date must not overwrite
        added = self.store.append_observations("BAMLH0A0HYM2", {"2026-08-07": 9.99})
        self.assertEqual(added, 0)
        self.assertEqual(self.store.get_series("BAMLH0A0HYM2"), [("2026-08-07", 3.02)])

    def test_cds_sanity_range(self):
        self.assertTrue(self.store.append_cds_observation("NVDA", 62.0, "2026-08-05", "https://x.com/a"))
        self.assertFalse(self.store.append_cds_observation("NVDA", 3.0, "2026-08-05", "https://x.com/b"))
        self.assertFalse(self.store.append_cds_observation("NVDA", 2500.0, "2026-08-05", "https://x.com/c"))
        self.assertEqual(len(self.store.get_cds_observations()), 1)

    def test_persistence_roundtrip(self):
        self.store.append_observations("LQD", {"2026-08-07": 108.5})
        self.store.save()
        reloaded = CreditHistoryStore(self.path)
        self.assertEqual(reloaded.get_series("LQD"), [("2026-08-07", 108.5)])


class TestDigestRendering(unittest.TestCase):
    def test_digest_contains_alerts_table_and_cds(self):
        snapshot = {
            "as_of": "2026-08-10T12:00:00+00:00",
            "alerts": [
                {"level": "ALERT", "code": "divergence_2007_style",
                 "message": "2007-style divergence: credit widening while QQQ is flat/up."},
                {"level": "WARN", "code": "spread_widening:BAMLC0A4CBBB",
                 "message": "BBB OAS widened +25bp over 5 trading days."},
            ],
            "series": [
                {"series_id": "BAMLC0A4CBBB", "name": "BBB OAS", "kind": "spread",
                 "unit": "bp", "level": 145.0, "d1": 5.0, "d1w": 25.0, "d1m": 25.0,
                 "accelerating": True, "stale": False, "last_date": "2026-08-10"},
                {"series_id": "LQD", "name": "LQD (proxy)", "kind": "etf",
                 "unit": "%", "level": 108.5, "d1": -0.2, "d1w": -1.1, "d1m": None,
                 "accelerating": False, "stale": True, "last_date": "2026-08-03"},
            ],
            "cds": [{"entity": "NVDA", "spread_bp": 62.0, "date": "2026-08-05",
                     "source_url": "https://example.com/nvda-cds"}],
            "notes": ["LQD: stale data as of 2026-08-03."],
        }
        text = render_digest(snapshot)
        self.assertIn("## Credit Monitor", text)
        # ALERT above WARN, alerts above the table
        self.assertLess(text.index("ALERT"), text.index("WARN"))
        self.assertLess(text.index("WARN"), text.index("BBB OAS "))
        self.assertIn("145bp", text)
        self.assertIn("+25bp", text)
        self.assertIn("accelerating", text)
        self.assertIn("stale as of 2026-08-03", text)
        self.assertIn("NVDA", text)
        self.assertIn("https://example.com/nvda-cds", text)

    def test_digest_never_crashes_on_empty_snapshot(self):
        text = render_digest({"alerts": [], "series": [], "cds": [], "notes": []})
        self.assertIn("No alerts.", text)


if __name__ == "__main__":
    unittest.main()
