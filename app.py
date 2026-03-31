"""
Macroeconomic Real-Time Dashboard – Flask Application

Start with:
    python app.py

The server will listen on port 5050 by default.
"""

from __future__ import annotations

import logging
import sys
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template
from apscheduler.schedulers.background import BackgroundScheduler

import config
from data_fetchers import DataCache

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("macro-dashboard")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.after_request
def _add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return response


# ---------------------------------------------------------------------------
# Shared cache
# ---------------------------------------------------------------------------
cache = DataCache()

# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler(daemon=True)


def _schedule_jobs():
    scheduler.add_job(
        cache.refresh_macro, "interval",
        seconds=config.REFRESH_INTERVALS["macro"],
        id="refresh_macro", replace_existing=True,
    )
    scheduler.add_job(
        cache.refresh_fedwatch, "interval",
        seconds=config.REFRESH_INTERVALS["fedwatch"],
        id="refresh_fedwatch", replace_existing=True,
    )
    scheduler.add_job(
        cache.refresh_truthsocial, "interval",
        seconds=config.REFRESH_INTERVALS["truthsocial"],
        id="refresh_truthsocial", replace_existing=True,
    )


# ---------------------------------------------------------------------------
# Data normalization helpers
# ---------------------------------------------------------------------------

# Map from FRED indicator names -> frontend keys
_MACRO_KEY_MAP = {
    "Total Nonfarm Payrolls": "nfp",
    "Unemployment Rate": "unemployment_rate",
    "Initial Jobless Claims": "initial_claims",
    "CPI All Urban Consumers": "cpi",
    "Core CPI (Less Food and Energy)": "core_cpi",
    "PCE Price Index": "pce",
    "Core PCE": "core_pce",
    "PPI Final Demand": "ppi",
    "Real GDP Growth Rate": "gdp",
    "Michigan Consumer Sentiment": "michigan_sentiment",
    "Advance Retail Sales": "retail_sales",
    "ISM Manufacturing PMI": "ism_manufacturing",
    "ISM Non-Manufacturing Business Activity": "ism_services",
    "Housing Starts": "housing_starts",
    "Building Permits": "building_permits",
    "New Home Sales": "new_home_sales",
    "Existing Home Sales": "existing_home_sales",
}


def _normalize_indicator(raw: dict) -> dict:
    """Convert a FRED indicator dict to the shape the frontend expects."""
    if "error" in raw:
        return {"value": None, "previous": None, "history": [], "last_date": None, "error": raw["error"]}
    history = []
    for h in raw.get("history", []):
        if h.get("value") is not None:
            history.append({"date": h["date"], "value": h["value"]})
    return {
        "value": raw.get("current"),
        "previous": raw.get("previous"),
        "change": raw.get("change"),
        "change_pct": raw.get("change_pct"),
        "yoy_change": raw.get("yoy_change"),
        "yoy_change_pct": raw.get("yoy_change_pct"),
        "last_date": raw.get("release_date"),
        "history": history,
        "series_id": raw.get("series_id"),
    }


def _normalize_macro(raw: dict) -> dict:
    """Reorganize raw FRED data into the structure the frontend expects."""
    if "_error" in raw:
        return {"_error": raw["_error"]}

    result = {}
    for category, indicators in raw.items():
        if category.startswith("_"):
            result[category] = indicators
            continue
        cat_data = {}
        for name, ind_raw in indicators.items():
            key = _MACRO_KEY_MAP.get(name, name.lower().replace(" ", "_"))
            cat_data[key] = _normalize_indicator(ind_raw)
        result[category] = cat_data

    # Also build a gdp_section wrapper for the GDP card + chart
    if "gdp" in result:
        gdp_ind = result["gdp"].get("gdp")
        if gdp_ind:
            result["gdp_section"] = {"gdp": gdp_ind}

    return result


def _normalize_fedwatch(raw: dict) -> dict:
    """Reshape FedWatch data for the frontend."""
    meetings_raw = raw.get("meetings", [])
    now = datetime.now(timezone.utc).date()

    meetings = []
    next_meeting = None
    days_to_next = None
    for m in meetings_raw:
        start = m.get("start", m.get("date", ""))
        end = m.get("end", "")
        label = f"{start} ~ {end}" if end else start
        meetings.append({"date": label, "start": start, "end": end})
        if next_meeting is None and start:
            try:
                mdate = datetime.strptime(start, "%Y-%m-%d").date()
                if mdate >= now:
                    next_meeting = label
                    days_to_next = (mdate - now).days
            except ValueError:
                pass

    return {
        "current_rate": raw.get("current_rate", "--"),
        "meetings": meetings,
        "probabilities": raw.get("probabilities"),
        "source_note": raw.get("source_note"),
        "next_meeting": next_meeting,
        "days_to_next": days_to_next,
    }


def _normalize_truthsocial(raw) -> dict:
    """Wrap Truth Social data in {posts: [...]} or {error: ...}."""
    if isinstance(raw, list):
        return {"posts": raw}
    if isinstance(raw, dict) and "error" in raw:
        return {"posts": [], "error": raw["error"]}
    return {"posts": [], "error": "Unexpected data format"}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", fred_api_configured=cache.fred.is_configured)


@app.route("/api/macro")
def api_macro():
    data = cache.get_macro()
    return jsonify(_normalize_macro(data))


@app.route("/api/fedwatch")
def api_fedwatch():
    data = cache.get_fedwatch()
    return jsonify(_normalize_fedwatch(data))


@app.route("/api/truthsocial")
def api_truthsocial():
    data = cache.get_truthsocial()
    return jsonify(_normalize_truthsocial(data))


@app.route("/api/status")
def api_status():
    return jsonify(cache.get_status())


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _print_banner():
    fred_ok = cache.fred.is_configured
    print("\n" + "=" * 64)
    print("  MACROECONOMIC REAL-TIME DASHBOARD")
    print("=" * 64)
    print(f"  Server      : http://localhost:{config.FLASK_PORT}")
    print(f"  FRED API key: {'configured' if fred_ok else 'NOT SET (macro data unavailable)'}")
    if not fred_ok:
        print("                Get a free key at:")
        print("                https://fred.stlouisfed.org/docs/api/api_key.html")
        print("                Set via FRED_API_KEY env var or in config.py")
    print()
    print("  Endpoints:")
    print(f"    Dashboard : http://localhost:{config.FLASK_PORT}/")
    print(f"    Macro API : http://localhost:{config.FLASK_PORT}/api/macro")
    print(f"    FedWatch  : http://localhost:{config.FLASK_PORT}/api/fedwatch")
    print(f"    Truth Soc : http://localhost:{config.FLASK_PORT}/api/truthsocial")
    print(f"    Status    : http://localhost:{config.FLASK_PORT}/api/status")
    print()
    print("  Refresh intervals:")
    for key, secs in config.REFRESH_INTERVALS.items():
        print(f"    {key:15s} every {secs}s ({secs // 60}m)")
    print("=" * 64 + "\n")


def main():
    _print_banner()
    _schedule_jobs()
    scheduler.start()
    logger.info("Background scheduler started.")

    def _initial_fetch():
        for label, fn in [
            ("macro", cache.refresh_macro),
            ("fedwatch", cache.refresh_fedwatch),
            ("truthsocial", cache.refresh_truthsocial),
        ]:
            try:
                fn()
                logger.info("Initial %s fetch complete.", label)
            except Exception as exc:
                logger.warning("Initial %s fetch failed: %s", label, exc)

    threading.Thread(target=_initial_fetch, daemon=True).start()

    app.run(
        host=config.FLASK_HOST,
        port=config.FLASK_PORT,
        debug=config.DEBUG,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
