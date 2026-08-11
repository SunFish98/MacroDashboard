"""
Memory Spot Price Monitor (storage-cycle tracker).

Scrapes DRAM/NAND spot prices from dramexchange.com and tracks momentum
as a daily thermometer for the memory cycle. Spot prices lead contract
prices at inflections, so the signal to watch is the weekly change and
whether it is accelerating or rolling over — not the level.

Reuses the append-only CreditHistoryStore for persistence, but the history
file is COMMITTED to git (config.MEMORY_HISTORY_FILE): there is no backfill
API for spot prices, so history only survives ephemeral cloud disks if the
repo carries it. scripts/refresh_snapshots.py appends and pushes it.

Run standalone:
    python memory_monitor.py                       # scrape + print digest
    python memory_monitor.py add DDR5_16G 51.60 2026-08-07   # manual entry

Pure functions (unit-tested):
    compute_calendar_signals – calendar-window deltas for sparse series
    evaluate_memory_alerts   – config-driven WARN logic (drop / rollover)
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup

import config
from credit_monitor import CreditHistoryStore, _HEADERS
from pathlib import Path

logger = logging.getLogger(__name__)

_DRAMEXCHANGE_URL = "https://www.dramexchange.com/"


# ---------------------------------------------------------------------------
# Signal computation (calendar windows — spot data can be sparse/irregular)
# ---------------------------------------------------------------------------

def _value_on_or_before(valid: list[tuple[str, float]], target: str, oldest: str) -> float | None:
    """Latest value dated <= target but >= oldest (keeps the base honest:
    a "1-week" delta must not silently compare against a months-old print)."""
    for d, v in reversed(valid):
        if d <= target:
            return v if d >= oldest else None
    return None


def compute_calendar_signals(observations: list[tuple[str, float]]) -> dict:
    """Deltas over calendar windows from ascending [(date, value), ...].

    Unlike credit_monitor.compute_signals (trading-day positions), windows
    here are calendar-based because spot observations may be daily scrapes
    or sparse manual entries (weekly/monthly). For each window the base is
    the closest observation at-or-before the target date, accepted only if
    it is no older than one extra window (else the delta is None).

    Returns {level, d1w, d1m, d3m, d1w_prev, accelerating, rolling_over,
    last_date} — deltas in %, None where history is insufficient.
    """
    valid = [(d, v) for d, v in observations if v is not None]
    out: dict[str, Any] = {
        "level": None, "d1w": None, "d1m": None, "d3m": None,
        "d1w_prev": None, "accelerating": False, "rolling_over": False,
        "last_date": None,
    }
    if not valid:
        return out
    last_date_s, last_value = valid[-1]
    out["level"] = round(last_value, 2)
    out["last_date"] = last_date_s
    try:
        last_date = datetime.strptime(last_date_s, "%Y-%m-%d").date()
    except ValueError:
        return out

    def pct_from(days_back: int, end_value: float, end_date) -> float | None:
        target = (end_date - timedelta(days=days_back)).isoformat()
        oldest = (end_date - timedelta(days=days_back * 2)).isoformat()
        base = _value_on_or_before(valid, target, oldest)
        if base in (None, 0):
            return None
        return round((end_value - base) / abs(base) * 100.0, 2)

    out["d1w"] = pct_from(7, last_value, last_date)
    out["d1m"] = pct_from(30, last_value, last_date)
    out["d3m"] = pct_from(91, last_value, last_date)

    # Previous week's weekly change, for the acceleration read
    prev_target = (last_date - timedelta(days=7)).isoformat()
    prev_oldest = (last_date - timedelta(days=14)).isoformat()
    prev_value = _value_on_or_before(valid, prev_target, prev_oldest)
    if prev_value is not None:
        prev_date = next(
            datetime.strptime(d, "%Y-%m-%d").date()
            for d, v in reversed(valid) if d <= prev_target
        )
        out["d1w_prev"] = pct_from(7, prev_value, prev_date)

    if out["d1w"] is not None and out["d1w_prev"] is not None:
        out["accelerating"] = out["d1w"] > out["d1w_prev"] > 0
    # Rollover: rising on the month, falling on the week — the earliest
    # cycle-top pattern (flagged here; WARN threshold applied in alerts).
    if out["d1w"] is not None and out["d1m"] is not None:
        out["rolling_over"] = out["d1w"] < 0 < out["d1m"]
    return out


def evaluate_memory_alerts(
    signals: dict[str, dict], thresholds: dict | None = None
) -> list[dict]:
    """Config-driven WARN evaluation for the memory cycle.

    signals: {series_id: compute_calendar_signals() result}
    Returns [{"level": "WARN", "code": ..., "message": ...}, ...]
    """
    t = thresholds or config.MEMORY_ALERT_THRESHOLDS
    alerts: list[dict] = []
    for series_id, sig in signals.items():
        name = config.MEMORY_SERIES.get(series_id, (series_id, None))[0]
        d1w, d1m = sig.get("d1w"), sig.get("d1m")
        if d1w is None:
            continue
        if d1w <= t["weekly_drop_pct"]:
            alerts.append({
                "level": "WARN",
                "code": f"memory_weekly_drop:{series_id}",
                "message": (
                    f"{name} spot fell {d1w:+.1f}% in a week "
                    f"(threshold {t['weekly_drop_pct']:.1f}%) — "
                    "spot rolling over is the earliest cycle-top warning"
                ),
            })
        elif d1w < 0 and d1m is not None and d1m >= t["rollover_monthly_min_pct"]:
            alerts.append({
                "level": "WARN",
                "code": f"memory_rollover:{series_id}",
                "message": (
                    f"{name}: weekly change turned negative ({d1w:+.1f}%) while "
                    f"still up {d1m:+.1f}% on the month — fresh rollover, watch "
                    "whether contract prices follow"
                ),
            })
    return alerts


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

class MemoryMonitorFetcher:
    """Scrape DRAMeXchange spot tables and build the monitor snapshot.

    DRAMeXchange does not block datacenter IPs (unlike CME/Truth Social),
    so live scraping works in the cloud too. History still comes from the
    committed store — cloud disks are ephemeral, so in-cloud appends only
    add today's point on top of the repo-carried history.
    """

    def __init__(self, store: CreditHistoryStore | None = None):
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)
        self.store = store or CreditHistoryStore(
            Path(__file__).parent / config.MEMORY_HISTORY_FILE
        )

    # ------------------------------------------------------------------
    def _sanity_ok(self, series_id: str, value: float) -> bool:
        lo, hi = config.MEMORY_SANITY_RANGE.get(
            series_id, config.MEMORY_SANITY_RANGE["_default"]
        )
        return lo <= value <= hi

    def fetch_spot_prices(self) -> dict[str, float]:
        """Scrape the current spot values. Returns {series_id: value}."""
        resp = self.session.get(_DRAMEXCHANGE_URL, timeout=20)
        resp.raise_for_status()
        html = resp.text
        out: dict[str, float] = {}

        # DXI index appears as e.g. >965,147.70< near the DXI label
        m = re.search(r'DXI"\s*>\s*([\d,]+\.?\d*)\s*<', html)
        if m:
            try:
                dxi = float(m.group(1).replace(",", ""))
                if self._sanity_ok("DXI", dxi):
                    out["DXI"] = dxi
            except ValueError:
                pass

        # Spot tables: rows of [Item, ..., Session Average, Change, ...]
        soup = BeautifulSoup(html, "html.parser")
        label_map = {
            prefix: sid
            for sid, (_name, prefix) in config.MEMORY_SERIES.items()
            if prefix
        }
        for table in soup.find_all("table"):
            header = [th.get_text(" ", strip=True) for th in table.find_all("th")]
            rows = table.find_all("tr")
            if not rows:
                continue
            first_row = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
            cols = header if "Session Average" in header else first_row
            if "Session Average" not in cols or "Item" not in cols:
                continue
            avg_idx = cols.index("Session Average")
            for tr in rows[1:]:
                cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if len(cells) <= avg_idx:
                    continue
                item = cells[0]
                for prefix, sid in label_map.items():
                    if item.startswith(prefix):
                        try:
                            value = float(cells[avg_idx].replace(",", ""))
                        except ValueError:
                            continue
                        if self._sanity_ok(sid, value):
                            out[sid] = round(value, 3)
        return out

    # ------------------------------------------------------------------
    def build_snapshot(self, refresh: bool = True) -> dict:
        """Scrape (optionally), compute signals/alerts, return snapshot.

        Never raises — a failed scrape degrades to stored history with a
        "stale data as of {date}" note.
        """
        notes: list[str] = []
        if refresh:
            try:
                prices = self.fetch_spot_prices()
                if prices:
                    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    for sid, value in prices.items():
                        self.store.append_observations(sid, {today: value})
                    self.store.save()
                else:
                    notes.append("DRAMeXchange scrape returned no rows — using stored history.")
            except Exception as exc:
                logger.warning("DRAMeXchange fetch failed: %s", exc)
                notes.append("DRAMeXchange unreachable — using stored history.")

        thresholds = config.MEMORY_ALERT_THRESHOLDS
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows: list[dict] = []
        signals: dict[str, dict] = {}
        for sid, (name, _prefix) in config.MEMORY_SERIES.items():
            obs = self.store.get_series(sid)
            sig = compute_calendar_signals(obs)
            signals[sid] = sig
            last = sig["last_date"]
            stale = (
                last is None
                or (datetime.strptime(today, "%Y-%m-%d").date()
                    - datetime.strptime(last, "%Y-%m-%d").date()).days
                > thresholds["stale_after_days"]
            )
            if last and stale:
                notes.append(f"{name}: stale data as of {last}.")
            rows.append({
                "series_id": sid, "name": name,
                "unit": "index" if sid == "DXI" else "usd",
                "stale": stale,
                "history": [{"date": d, "value": v} for d, v in obs[-90:]],
                **sig,
            })

        alerts = evaluate_memory_alerts(signals, thresholds)
        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "alerts": alerts,
            "series": rows,
            "notes": notes,
            "source": _DRAMEXCHANGE_URL,
        }


# ---------------------------------------------------------------------------
# Digest rendering
# ---------------------------------------------------------------------------

def render_digest(snapshot: dict) -> str:
    """Render the Memory Monitor digest section as compact text."""
    lines: list[str] = ["## Memory Spot Monitor"]
    for alert in snapshot["alerts"]:
        lines.append(f"🟡 {alert['level']}: {alert['message']}")
    if not snapshot["alerts"]:
        lines.append("No alerts — spot momentum intact.")
    lines.append("")

    def fmt(v: float | None) -> str:
        return "—" if v is None else f"{v:+.1f}%"

    lines.append(f"{'Series':<24} {'Level':>12} {'Δ1w':>7} {'Δ1m':>7} {'Δ3m':>7}  Note")
    for row in snapshot["series"]:
        level = "—"
        if row["level"] is not None:
            level = f"{row['level']:,.0f}" if row["unit"] == "index" else f"${row['level']:.2f}"
        note_bits = []
        if row.get("accelerating"):
            note_bits.append("accelerating")
        if row.get("rolling_over"):
            note_bits.append("ROLLING OVER")
        if row.get("stale") and row.get("last_date"):
            note_bits.append(f"stale as of {row['last_date']}")
        lines.append(
            f"{row['name']:<24} {level:>12} {fmt(row['d1w']):>7} "
            f"{fmt(row['d1m']):>7} {fmt(row['d3m']):>7}  " + ", ".join(note_bits)
        )
    lines.append("")
    lines.append(f"· Source: {snapshot.get('source', _DRAMEXCHANGE_URL)} (spot; contract prices are the confirm)")
    return "\n".join(lines).rstrip()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    fetcher = MemoryMonitorFetcher()
    if len(sys.argv) >= 2 and sys.argv[1] == "add":
        # Manual point-in-time entry: add SERIES_ID PRICE [DATE]
        if len(sys.argv) < 4:
            print("Usage: python memory_monitor.py add SERIES_ID PRICE [YYYY-MM-DD]")
            return 1
        sid, price = sys.argv[2], float(sys.argv[3])
        date = sys.argv[4] if len(sys.argv) > 4 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if sid not in config.MEMORY_SERIES:
            print(f"Unknown series {sid}. Known: {', '.join(config.MEMORY_SERIES)}")
            return 1
        added = fetcher.store.append_observations(sid, {date: price})
        fetcher.store.save()
        print(f"{'Added' if added else 'Already recorded (append-only)'}: {sid} {price} @ {date}")
        return 0
    snapshot = fetcher.build_snapshot(refresh=True)
    print()
    print(render_digest(snapshot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
