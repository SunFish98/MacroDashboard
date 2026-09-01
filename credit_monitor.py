"""
AI-Cycle Credit Risk Monitor.

Tracks credit spreads as an early-warning signal for stress in AI
infrastructure debt (thesis: credit markets reprice risk before equity
markets do).

Classes:
    CreditHistoryStore   – append-only JSON store of daily observations
    CreditMonitorFetcher – pulls FRED OAS series + credit ETF proxies,
                           computes signals/alerts, renders the digest

Pure functions (unit-tested against fixtures):
    compute_signals      – level + Δ1d / Δ1w / Δ1m + acceleration flag
    evaluate_alerts      – config-driven WARN/ALERT logic incl. divergence

Run standalone to print the digest section:
    python credit_monitor.py
"""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/csv,*/*;q=0.8",
}

_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_STOOQ_CSV_URL = "https://stooq.com/q/d/l/"


def fetch_remote_snapshot(filename: str):
    """Fetch a JSON snapshot published on the snapshots branch (see
    config.SNAPSHOT_REMOTE_BASE). Returns parsed JSON, or None on any
    failure — callers fall back to their local cache/ copy.

    Lives here (not data_fetchers) so memory_monitor can use it without an
    import cycle.
    """
    base = getattr(config, "SNAPSHOT_REMOTE_BASE", None)
    if not base:
        return None
    url = base.rstrip("/") + "/cache/" + filename
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=10)
        if resp.status_code != 200:
            logger.info("Remote snapshot %s returned %s", filename, resp.status_code)
            return None
        return resp.json()
    except Exception as exc:
        logger.info("Remote snapshot fetch failed for %s: %s", filename, exc)
        return None


# ---------------------------------------------------------------------------
# Append-only observation store
# ---------------------------------------------------------------------------

class CreditHistoryStore:
    """Append-only JSON store of daily observations, keyed by series + date.

    File layout:
        {
          "series": {"BAMLH0A0HYM2": {"2026-08-07": 3.02, ...}, "LQD": {...}},
          "cds_observations": [
            {"entity": "NVDA", "spread_bp": 62.0, "date": "2026-08-05",
             "source_url": "https://...", "fetched_at": "..."}
          ],
          "meta": {"last_update": "...", "backfilled_series": [...]}
        }

    Existing (series, date) values are never overwritten — observations are
    point-in-time and append-only.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or Path(__file__).parent / config.CREDIT_HISTORY_FILE)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            if self.path.exists():
                with open(self.path) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("series", {})
                    data.setdefault("cds_observations", [])
                    data.setdefault("meta", {})
                    return data
        except Exception as exc:
            logger.warning("Failed to load credit history %s: %s", self.path, exc)
        return {"series": {}, "cds_observations": [], "meta": {}}

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump(self._data, f, indent=1, sort_keys=True)
            tmp.replace(self.path)

    # ------------------------------------------------------------------
    def append_observations(self, series_id: str, observations: dict[str, float]) -> int:
        """Add {date: value} pairs for a series; existing dates are kept as-is.

        Returns the number of newly appended observations.
        """
        added = 0
        with self._lock:
            bucket = self._data["series"].setdefault(series_id, {})
            for date, value in observations.items():
                if value is None or date in bucket:
                    continue
                bucket[date] = value
                added += 1
            if added:
                self._data["meta"]["last_update"] = datetime.now(timezone.utc).isoformat()
        return added

    def get_series(self, series_id: str) -> list[tuple[str, float]]:
        """Return [(date, value), ...] sorted ascending by date."""
        with self._lock:
            bucket = self._data["series"].get(series_id, {})
            return sorted(bucket.items())

    def has_backfill(self, series_id: str) -> bool:
        with self._lock:
            return series_id in self._data["meta"].get("backfilled_series", [])

    def mark_backfilled(self, series_id: str) -> None:
        with self._lock:
            done = self._data["meta"].setdefault("backfilled_series", [])
            if series_id not in done:
                done.append(series_id)

    # ------------------------------------------------------------------
    def append_cds_observation(
        self, entity: str, spread_bp: float, date: str, source_url: str
    ) -> bool:
        """Store a point-in-time single-name CDS reading with attribution.

        Duplicates (same entity + date + spread) are ignored. Readings
        outside the 5–2000bp sanity range are rejected.
        """
        if not (5.0 <= spread_bp <= 2000.0):
            logger.info("Rejected CDS reading outside sanity range: %s %.1fbp", entity, spread_bp)
            return False
        obs = {
            "entity": entity.upper(),
            "spread_bp": round(float(spread_bp), 1),
            "date": date,
            "source_url": source_url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            for existing in self._data["cds_observations"]:
                if (existing["entity"] == obs["entity"] and existing["date"] == obs["date"]
                        and existing["spread_bp"] == obs["spread_bp"]):
                    return False
            self._data["cds_observations"].append(obs)
        return True

    def get_cds_observations(self) -> list[dict]:
        """All CDS observations sorted ascending by date."""
        with self._lock:
            return sorted(self._data["cds_observations"], key=lambda o: o["date"])


# ---------------------------------------------------------------------------
# Signal computation (pure, unit-tested)
# ---------------------------------------------------------------------------

def compute_signals(
    observations: list[tuple[str, float]], unit: str = "bp",
    risk_direction: str = "up",
) -> dict:
    """Compute level and deltas from ascending [(date, value), ...].

    Windows are measured in TRADING days (observation positions), which
    handles market holidays and FRED's skipped dates naturally.

    unit "bp":  values are OAS percent -> level/deltas reported in bp.
    unit "pct": values are prices      -> deltas reported as % change.

    risk_direction sets what the "accelerating" flag tracks: "up" for
    spreads (widening = risk), "down" for bond ETF prices (falling = risk).

    Returns {level, d1, d1w, d1m, d1w_prev, accelerating, last_date} with
    None where history is insufficient.
    """
    valid = [(d, v) for d, v in observations if v is not None]
    out: dict[str, Any] = {
        "level": None, "d1": None, "d1w": None, "d1m": None,
        "d1w_prev": None, "accelerating": False, "last_date": None,
    }
    if not valid:
        return out

    scale = 100.0 if unit == "bp" else 1.0
    values = [v * scale for _, v in valid]
    out["last_date"] = valid[-1][0]
    out["level"] = round(values[-1], 1)

    def delta(back: int, end: int = 0) -> float | None:
        """Change from `back` trading days before position -1-end to it."""
        i_end = len(values) - 1 - end
        i_start = i_end - back
        if i_start < 0 or i_end < 0:
            return None
        if unit == "pct":
            base = values[i_start]
            if base == 0:
                return None
            return round((values[i_end] - base) / abs(base) * 100.0, 2)
        return round(values[i_end] - values[i_start], 1)

    out["d1"] = delta(1)
    out["d1w"] = delta(5)
    out["d1m"] = delta(21)
    out["d1w_prev"] = delta(5, end=5)  # the week before this one

    # Rate-of-change flag: is this week's risk move faster than last week's?
    # (First derivative matters more than level.)
    if out["d1w"] is not None and out["d1w_prev"] is not None:
        if risk_direction == "down":
            out["accelerating"] = out["d1w"] < out["d1w_prev"] and out["d1w"] < 0
        else:
            out["accelerating"] = out["d1w"] > out["d1w_prev"] and out["d1w"] > 0
    return out


def is_stale(last_date: str | None, today: str, stale_after_days: int) -> bool:
    """True if the newest observation is older than the allowed lag.

    FRED publishes T+1, so `stale_after_days` must absorb weekends and
    market holidays (default 5 calendar days).
    """
    if not last_date:
        return True
    try:
        last = datetime.strptime(last_date, "%Y-%m-%d").date()
        now = datetime.strptime(today, "%Y-%m-%d").date()
    except ValueError:
        return True
    return (now - last).days > stale_after_days


def evaluate_alerts(
    spread_signals: dict[str, dict],
    cds_observations: list[dict],
    equity_signal: dict | None,
    thresholds: dict | None = None,
) -> list[dict]:
    """Config-driven WARN/ALERT evaluation.

    spread_signals: {series_id: signal dict from compute_signals (bp)}
    cds_observations: ascending-by-date list of CDS readings
    equity_signal: compute_signals() result (unit "pct") for the divergence
                   benchmark (QQQ), or None if unavailable.

    Returns alerts sorted ALERT-first, each:
        {"level": "WARN"|"ALERT", "code": ..., "message": ...}
    """
    t = thresholds or config.CREDIT_ALERT_THRESHOLDS
    alerts: list[dict] = []

    # 1. Index spread widening over the rolling window
    spread_warned = False
    for series_id, limit_bp in t.get("spread_widening_bp", {}).items():
        sig = spread_signals.get(series_id)
        if not sig or sig.get("d1w") is None:
            continue
        if sig["d1w"] > limit_bp:
            spread_warned = True
            name = config.CREDIT_FRED_SERIES.get(series_id, series_id)
            alerts.append({
                "level": "WARN",
                "code": f"spread_widening:{series_id}",
                "message": (
                    f"{name} widened {sig['d1w']:+.0f}bp over "
                    f"{t['window_trading_days']} trading days "
                    f"(threshold {limit_bp:.0f}bp), now {sig['level']:.0f}bp"
                    + (" — accelerating" if sig.get("accelerating") else "")
                ),
            })

    # 2. Single-name CDS levels and short-window widening
    latest_by_entity: dict[str, list[dict]] = {}
    for obs in cds_observations:
        latest_by_entity.setdefault(obs["entity"], []).append(obs)

    for entity, series in latest_by_entity.items():
        latest = series[-1]
        limit = t.get("cds_level_bp", {}).get(entity)
        if limit is not None and latest["spread_bp"] > limit:
            alerts.append({
                "level": "WARN",
                "code": f"cds_level:{entity}",
                "message": (
                    f"{entity} 5Y CDS at {latest['spread_bp']:.0f}bp "
                    f"(threshold {limit:.0f}bp) as of {latest['date']}"
                ),
            })
        if len(series) >= 2:
            prev = series[-2]
            try:
                gap = (datetime.strptime(latest["date"], "%Y-%m-%d")
                       - datetime.strptime(prev["date"], "%Y-%m-%d")).days
            except ValueError:
                gap = 99
            widening = latest["spread_bp"] - prev["spread_bp"]
            if 0 <= gap <= 3 and widening > t.get("cds_1d_widening_bp", 15.0):
                alerts.append({
                    "level": "WARN",
                    "code": f"cds_widening:{entity}",
                    "message": (
                        f"{entity} 5Y CDS widened {widening:+.0f}bp "
                        f"({prev['date']} → {latest['date']})"
                    ),
                })

    # 3. Divergence: credit stress while equities shrug it off
    if spread_warned and equity_signal and equity_signal.get("d1w") is not None:
        flat_pct = t.get("divergence_equity_flat_pct", 0.0)
        if equity_signal["d1w"] >= flat_pct:
            alerts.append({
                "level": "ALERT",
                "code": "divergence_2007_style",
                "message": (
                    "2007-style divergence: credit spreads widening while "
                    f"{config.CREDIT_EQUITY_BENCHMARK} is flat/up "
                    f"({equity_signal['d1w']:+.1f}% over the same window). "
                    "Credit is repricing risk that equities are ignoring."
                ),
            })

    alerts.sort(key=lambda a: 0 if a["level"] == "ALERT" else 1)
    return alerts


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

class CreditMonitorFetcher:
    """Fetch credit-spread series + ETF proxies and build the monitor snapshot.

    Sources (each degrades independently — a dead source never crashes the
    digest, it just yields "stale data as of {date}" from the local store):
      1. FRED OAS series (needs FRED_API_KEY; 2-year backfill on first run)
      2. Yahoo Finance chart API for LQD/HYG/QQQ closes (Stooq CSV fallback)
      3. Single-name CDS readings from the store (news scraping not wired
         up — see README)
    """

    def __init__(self, api_key: str | None = None, store: CreditHistoryStore | None = None):
        self.api_key = api_key or config.FRED_API_KEY
        self.session = requests.Session()
        self.session.headers.update(_HEADERS)
        self.store = store or CreditHistoryStore()

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key) and self.api_key != "YOUR_FRED_API_KEY_HERE"

    # ------------------------------------------------------------------
    # FRED spreads
    # ------------------------------------------------------------------
    def fetch_fred_series(self, series_id: str, start_date: str | None = None) -> dict[str, float]:
        """Return {date: value} for a FRED series (values in percent)."""
        if not self.is_configured:
            raise RuntimeError(
                "FRED API key is not configured. Get a free key at "
                "https://fred.stlouisfed.org/docs/api/api_key.html and set "
                "FRED_API_KEY in config.py or as an environment variable."
            )
        params: dict[str, Any] = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": "asc",
        }
        if start_date:
            params["observation_start"] = start_date
        resp = self.session.get(config.FRED_BASE_URL, params=params, timeout=20)
        resp.raise_for_status()
        out: dict[str, float] = {}
        for obs in resp.json().get("observations", []):
            raw = obs.get("value", ".")
            if raw in (".", "", None):
                continue
            try:
                out[obs["date"]] = float(raw)
            except (ValueError, TypeError):
                continue
        return out

    def _refresh_fred(self, notes: list[str]) -> None:
        if not self.is_configured:
            notes.append(
                "FRED API key not configured — spread data limited to stored history. "
                "Get a free key: https://fred.stlouisfed.org/docs/api/api_key.html"
            )
            return
        for series_id in config.CREDIT_FRED_SERIES:
            try:
                if not self.store.has_backfill(series_id):
                    # First run: backfill ~2 years so deltas work immediately
                    start = (datetime.now(timezone.utc)
                             - timedelta(days=config.CREDIT_BACKFILL_DAYS)).strftime("%Y-%m-%d")
                    obs = self.fetch_fred_series(series_id, start_date=start)
                    self.store.append_observations(series_id, obs)
                    self.store.mark_backfilled(series_id)
                    logger.info("Backfilled %s: %d observations", series_id, len(obs))
                else:
                    # Incremental: last 2 weeks covers revisions after holidays
                    start = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")
                    obs = self.fetch_fred_series(series_id, start_date=start)
                    added = self.store.append_observations(series_id, obs)
                    if added:
                        logger.info("Appended %d new observations for %s", added, series_id)
            except Exception as exc:
                logger.warning("FRED fetch failed for %s: %s", series_id, exc)
                notes.append(f"FRED fetch failed for {series_id} — using stored history.")

    # ------------------------------------------------------------------
    # ETF proxies (Yahoo primary, Stooq fallback)
    # ------------------------------------------------------------------
    def fetch_etf_history(self, symbol: str, range_: str = "3mo") -> dict[str, float]:
        """Return {date: close} for a ticker; tries Yahoo then Stooq."""
        closes = self._try_yahoo(symbol, range_)
        if closes:
            return closes
        closes = self._try_stooq(symbol)
        if closes:
            return closes
        return {}

    def _try_yahoo(self, symbol: str, range_: str) -> dict[str, float] | None:
        url = _YAHOO_CHART_URL.format(symbol=symbol)
        for attempt in range(3):
            try:
                resp = self.session.get(
                    url, params={"range": range_, "interval": "1d"}, timeout=15
                )
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                if resp.status_code != 200:
                    logger.info("Yahoo chart API returned %s for %s", resp.status_code, symbol)
                    return None
                result = (resp.json().get("chart", {}).get("result") or [None])[0]
                if not result:
                    return None
                timestamps = result.get("timestamp") or []
                quotes = (result.get("indicators", {}).get("quote") or [{}])[0]
                closes = quotes.get("close") or []
                out: dict[str, float] = {}
                for ts, close in zip(timestamps, closes):
                    if close is None:
                        continue
                    date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                    out[date] = round(float(close), 2)
                return out or None
            except Exception as exc:
                logger.info("Yahoo fetch failed for %s: %s", symbol, exc)
                return None
        logger.info("Yahoo chart API rate-limited for %s", symbol)
        return None

    def _try_stooq(self, symbol: str) -> dict[str, float] | None:
        try:
            resp = self.session.get(
                _STOOQ_CSV_URL, params={"s": f"{symbol.lower()}.us", "i": "d"}, timeout=15
            )
            if resp.status_code != 200 or "Date" not in resp.text[:200]:
                return None
            out: dict[str, float] = {}
            for row in csv.DictReader(io.StringIO(resp.text)):
                try:
                    out[row["Date"]] = round(float(row["Close"]), 2)
                except (KeyError, ValueError):
                    continue
            return out or None
        except Exception as exc:
            logger.info("Stooq fetch failed for %s: %s", symbol, exc)
            return None

    def _refresh_etfs(self, notes: list[str]) -> None:
        symbols = list(config.CREDIT_ETF_PROXIES) + [config.CREDIT_EQUITY_BENCHMARK]
        for symbol in symbols:
            try:
                range_ = "3mo" if self.store.get_series(symbol) else "2y"
                closes = self.fetch_etf_history(symbol, range_=range_)
                if closes:
                    self.store.append_observations(symbol, closes)
                else:
                    notes.append(f"Market data unavailable for {symbol} — using stored history.")
            except Exception as exc:
                logger.warning("ETF fetch failed for %s: %s", symbol, exc)
                notes.append(f"Market data unavailable for {symbol} — using stored history.")

    # ------------------------------------------------------------------
    # Snapshot + digest
    # ------------------------------------------------------------------
    def build_snapshot(self, refresh: bool = True) -> dict:
        """Fetch (optionally), compute signals/alerts, return the snapshot dict.

        Never raises — every failure degrades to stored history plus a note.
        """
        notes: list[str] = []
        if refresh:
            self._refresh_fred(notes)
            self._refresh_etfs(notes)
            try:
                self.store.save()
            except Exception as exc:
                logger.warning("Failed to persist credit history: %s", exc)

        thresholds = config.CREDIT_ALERT_THRESHOLDS
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rows: list[dict] = []
        spread_signals: dict[str, dict] = {}
        for series_id, name in config.CREDIT_FRED_SERIES.items():
            obs = self.store.get_series(series_id)
            sig = compute_signals(obs, unit="bp")
            spread_signals[series_id] = sig
            stale = is_stale(sig["last_date"], today, thresholds["stale_after_days"])
            if stale and sig["last_date"]:
                notes.append(f"{name}: stale data as of {sig['last_date']}.")
            elif not obs:
                notes.append(f"{name}: no data yet (FRED key needed for first fetch).")
            rows.append({
                "series_id": series_id, "name": name, "kind": "spread", "unit": "bp",
                "stale": stale,
                "history": [{"date": d, "value": round(v * 100, 1)} for d, v in obs[-60:]],
                **sig,
            })

        etf_signals: dict[str, dict] = {}
        for symbol in config.CREDIT_ETF_PROXIES:
            obs = self.store.get_series(symbol)
            sig = compute_signals(obs, unit="pct", risk_direction="down")
            etf_signals[symbol] = sig
            stale = is_stale(sig["last_date"], today, thresholds["stale_after_days"])
            if stale and sig["last_date"]:
                notes.append(f"{symbol}: stale data as of {sig['last_date']}.")
            rows.append({
                "series_id": symbol, "name": f"{symbol} (proxy)", "kind": "etf", "unit": "%",
                "stale": stale,
                "history": [{"date": d, "value": v} for d, v in obs[-60:]],
                **sig,
            })

        equity_signal = compute_signals(
            self.store.get_series(config.CREDIT_EQUITY_BENCHMARK), unit="pct"
        )
        cds_obs = self.store.get_cds_observations()
        alerts = evaluate_alerts(spread_signals, cds_obs, equity_signal, thresholds)

        # Most recent reading per entity, newest first, for the digest
        latest_cds: dict[str, dict] = {}
        for obs in cds_obs:
            latest_cds[obs["entity"]] = obs
        cds_rows = sorted(latest_cds.values(), key=lambda o: o["date"], reverse=True)

        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "alerts": alerts,
            "series": rows,
            "equity_benchmark": {
                "symbol": config.CREDIT_EQUITY_BENCHMARK,
                "d1w_pct": equity_signal.get("d1w"),
                "last_date": equity_signal.get("last_date"),
            },
            "cds": cds_rows,
            "notes": notes,
        }


# ---------------------------------------------------------------------------
# Digest rendering (compact text — dashboard row, not an essay)
# ---------------------------------------------------------------------------

def render_digest(snapshot: dict) -> str:
    """Render the Credit Monitor digest section as compact text."""
    lines: list[str] = ["## Credit Monitor"]

    for alert in snapshot["alerts"]:
        icon = "🔴" if alert["level"] == "ALERT" else "🟡"
        lines.append(f"{icon} {alert['level']}: {alert['message']}")
    if not snapshot["alerts"]:
        lines.append("No alerts.")
    lines.append("")

    def fmt(value: float | None, unit: str) -> str:
        if value is None:
            return "—"
        if unit == "%":
            return f"{value:+.2f}%"
        return f"{value:+.0f}bp"

    lines.append(f"{'Series':<14} {'Level':>9} {'Δ1d':>8} {'Δ1w':>8} {'Δ1m':>8}  Note")
    for row in snapshot["series"]:
        level = "—"
        if row["level"] is not None:
            level = f"{row['level']:.0f}bp" if row["unit"] == "bp" else f"{row['level']:.2f}"
        note_bits = []
        if row.get("accelerating"):
            note_bits.append("accelerating")
        if row.get("stale") and row.get("last_date"):
            note_bits.append(f"stale as of {row['last_date']}")
        elif row.get("stale"):
            note_bits.append("no data")
        lines.append(
            f"{row['name']:<14} {level:>9} {fmt(row['d1'], row['unit']):>8} "
            f"{fmt(row['d1w'], row['unit']):>8} {fmt(row['d1m'], row['unit']):>8}  "
            + ", ".join(note_bits)
        )

    if snapshot["cds"]:
        lines.append("")
        lines.append("Single-name 5Y CDS (news-sourced, point-in-time):")
        for obs in snapshot["cds"]:
            lines.append(
                f"  {obs['entity']:<6} {obs['spread_bp']:>6.0f}bp  {obs['date']}  {obs['source_url']}"
            )

    extra_notes = [n for n in snapshot["notes"]]
    if extra_notes:
        lines.append("")
        for note in extra_notes:
            lines.append(f"· {note}")
    return "\n".join(lines).rstrip()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    fetcher = CreditMonitorFetcher()
    snapshot = fetcher.build_snapshot(refresh=True)
    print()
    print(render_digest(snapshot))


if __name__ == "__main__":
    main()
