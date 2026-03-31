"""
Configuration for the Macroeconomic Real-Time Dashboard.

To use this dashboard, you need a free FRED API key.
Get one at: https://fred.stlouisfed.org/docs/api/api_key.html
Then set it below or via the FRED_API_KEY environment variable.
"""

import os

# ---------------------------------------------------------------------------
# FRED API
# ---------------------------------------------------------------------------
FRED_API_KEY = os.environ.get("FRED_API_KEY", "YOUR_FRED_API_KEY_HERE")
FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

# ---------------------------------------------------------------------------
# Refresh intervals (seconds) used by the background scheduler
# ---------------------------------------------------------------------------
REFRESH_INTERVALS = {
    "macro": 900,        # 15 minutes
    "fedwatch": 1800,    # 30 minutes
    "truthsocial": 300,  # 5 minutes
}

# ---------------------------------------------------------------------------
# Cache TTLs (seconds) – mirrors the refresh intervals
# ---------------------------------------------------------------------------
CACHE_TTL = {
    "macro": 900,
    "fedwatch": 1800,
    "truthsocial": 300,
}

# ---------------------------------------------------------------------------
# FRED Series IDs organised by category
#
# Each entry: series_id -> (display_name, display_mode)
#   display_mode:
#     "raw"      – show the value as-is (unemployment %, GDP %, PMI index, etc.)
#     "yoy_pct"  – value is a price index; compute & show YoY % change
#     "mom_diff" – show month-over-month absolute change (e.g. NFP +256K)
#     "mom_pct"  – show month-over-month % change (e.g. Retail Sales +0.4%)
# ---------------------------------------------------------------------------
FRED_SERIES: dict[str, dict[str, tuple[str, str]]] = {
    "employment": {
        "PAYEMS":  ("Total Nonfarm Payrolls", "mom_diff"),
        "UNRATE":  ("Unemployment Rate", "raw"),
        "ICSA":    ("Initial Jobless Claims", "raw"),
    },
    "inflation": {
        "CPIAUCSL": ("CPI All Urban Consumers", "yoy_pct"),
        "CPILFESL": ("Core CPI (Less Food and Energy)", "yoy_pct"),
        "PCEPI":    ("PCE Price Index", "yoy_pct"),
        "PCEPILFE": ("Core PCE", "yoy_pct"),
        "PPIFIS":   ("PPI Final Demand", "yoy_pct"),
    },
    "gdp": {
        "A191RL1Q225SBEA": ("Real GDP Growth Rate", "raw"),
    },
    "pmi_retail": {
        "UMCSENT":  ("Michigan Consumer Sentiment", "raw"),
        "RSAFS":    ("Advance Retail Sales", "mom_pct"),
        "NAPM":     ("ISM Manufacturing PMI", "raw"),
        "NMFBAI":   ("ISM Non-Manufacturing Business Activity", "raw"),
    },
    "housing": {
        "HOUST":          ("Housing Starts", "raw"),
        "PERMIT":         ("Building Permits", "raw"),
        "HSN1F":          ("New Home Sales", "raw"),
        "EXHOSLUSM495S":  ("Existing Home Sales", "raw"),
    },
}

# Flat lookups built from the nested dict
SERIES_NAME_MAP: dict[str, str] = {}
SERIES_DISPLAY_MODE: dict[str, str] = {}
for _cat, _series in FRED_SERIES.items():
    for _sid, (_name, _mode) in _series.items():
        SERIES_NAME_MAP[_sid] = _name
        SERIES_DISPLAY_MODE[_sid] = _mode

# ---------------------------------------------------------------------------
# FOMC Meeting Dates (two-day meetings, listed as start-end)
# ---------------------------------------------------------------------------
FOMC_DATES_2025 = [
    ("2025-01-28", "2025-01-29"),
    ("2025-03-18", "2025-03-19"),
    ("2025-05-06", "2025-05-07"),
    ("2025-06-17", "2025-06-18"),
    ("2025-07-29", "2025-07-30"),
    ("2025-09-16", "2025-09-17"),
    ("2025-10-28", "2025-10-29"),
    ("2025-12-09", "2025-12-10"),
]

FOMC_DATES_2026 = [
    ("2026-01-27", "2026-01-28"),
    ("2026-03-17", "2026-03-18"),
    ("2026-05-05", "2026-05-06"),
    ("2026-06-16", "2026-06-17"),
    ("2026-07-28", "2026-07-29"),
    ("2026-09-15", "2026-09-16"),
    ("2026-10-27", "2026-10-28"),
    ("2026-12-08", "2026-12-09"),
]

# ---------------------------------------------------------------------------
# Flask / Server
# ---------------------------------------------------------------------------
FLASK_HOST = "0.0.0.0"
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5050"))
DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
