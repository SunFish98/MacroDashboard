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
    "credit": 3600,      # 60 minutes (FRED spread data updates once a day, T+1)
}

# ---------------------------------------------------------------------------
# Cache TTLs (seconds) – mirrors the refresh intervals
# ---------------------------------------------------------------------------
CACHE_TTL = {
    "macro": 900,
    "fedwatch": 1800,
    "truthsocial": 300,
    "credit": 3600,
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
# Credit Risk Monitor (AI-cycle credit stress early warning)
#
# Thesis: credit markets reprice risk before equity markets do. Track index
# OAS spreads (FRED) + credit ETF proxies as an early-warning signal for
# stress in AI infrastructure debt. Uses the same FRED_API_KEY as above.
# ---------------------------------------------------------------------------

# FRED credit-spread series. Values arrive as OAS in PERCENT; the monitor
# converts them to basis points. Each entry: series_id -> display_name.
CREDIT_FRED_SERIES: dict[str, str] = {
    "BAMLC0A0CM":   "IG Corp OAS",   # ICE BofA US Corporate Index OAS (investment grade)
    "BAMLH0A0HYM2": "HY OAS",        # ICE BofA US High Yield OAS
    "BAMLC0A4CBBB": "BBB OAS",       # ICE BofA BBB US Corporate Index OAS
}

# Real-time credit-risk ETF proxies (daily close; Yahoo chart API with a
# Stooq CSV fallback — no API key needed). Signals are % changes, not bp.
CREDIT_ETF_PROXIES: list[str] = ["LQD", "HYG"]

# Equity benchmark used for the divergence signal (credit widening while
# equities are flat/up = "2007-style divergence").
CREDIT_EQUITY_BENCHMARK = "QQQ"

# Single-name CDS entities of interest (5Y CDS, bp). There is no free CDS
# API; observations are stored point-in-time with source attribution when
# available (see README — automated news scraping is currently not wired up).
CREDIT_CDS_ENTITIES: list[str] = ["NVDA", "ORCL", "GOOGL", "META", "AMZN"]

# How much FRED history to backfill on first run (calendar days).
CREDIT_BACKFILL_DAYS = 730  # ~2 years so deltas work immediately

# Append-only observation store (JSON, keyed by series -> date -> value).
CREDIT_HISTORY_FILE = "cache/credit_history.json"

# Alert thresholds — tune here, not in code.
CREDIT_ALERT_THRESHOLDS = {
    # Rolling window for widening checks, in TRADING days.
    "window_trading_days": 5,
    # WARN if the series widens by more than this many bp within the window.
    "spread_widening_bp": {
        "BAMLC0A4CBBB": 20.0,   # BBB OAS
        "BAMLH0A0HYM2": 50.0,   # HY OAS
    },
    # WARN if a single-name 5Y CDS observation exceeds this level (bp).
    "cds_level_bp": {
        "NVDA": 100.0,
        "ORCL": 250.0,
    },
    # WARN if any single name widens more than this (bp) vs its previous
    # observation (only applied when the two observations are <= 3 days apart).
    "cds_1d_widening_bp": 15.0,
    # Divergence: a spread WARN escalates to ALERT ("2007-style divergence")
    # when the equity benchmark's return over the same window is >= this (%).
    "divergence_equity_flat_pct": 0.0,
    # Observations older than this many calendar days are flagged stale
    # (FRED publishes T+1, so weekends/holidays must not trip this).
    "stale_after_days": 5,
}

# ---------------------------------------------------------------------------
# Memory Spot Price Monitor (storage-cycle tracker)
#
# Tracks DRAM/NAND spot prices from DRAMeXchange as a daily thermometer for
# the memory cycle. Spot leads contract prices at inflections; the signal
# to watch is momentum (weekly change and its acceleration), not level.
# ---------------------------------------------------------------------------

# Series to track. Each entry: series_id -> (display_name, row_label_prefix)
# row_label_prefix matches the "Item" cell on dramexchange.com's spot tables
# (None = the DXI index, parsed separately).
MEMORY_SERIES: dict[str, tuple[str, str | None]] = {
    "DXI":         ("DXI Index", None),
    "DDR5_16G":    ("DDR5 16Gb 4800/5600", "DDR5 16Gb (2Gx8) 4800/5600"),
    "NAND_512G":   ("NAND 512Gb TLC wafer", "512Gb TLC"),
    "NAND_256G":   ("NAND 256Gb TLC wafer", "256Gb TLC"),
    "NAND_2G_SLC": ("NAND 2Gb SLC (legacy)", "SLC 2Gb 256MBx8"),
}

# Append-only observation store. Committed to git (unlike credit history)
# because there is no backfill API — history only exists if we keep it, and
# cloud disks are ephemeral. The scripts/refresh_snapshots.py job pushes it.
MEMORY_HISTORY_FILE = "cache/memory_history.json"

# Sanity ranges for scraped values (reject obvious parse garbage).
MEMORY_SANITY_RANGE = {
    "DXI": (10_000.0, 10_000_000.0),
    "_default": (0.5, 2000.0),   # USD spot prices
}

MEMORY_ALERT_THRESHOLDS = {
    # WARN if a series falls more than this (%) over ~1 week — spot prices
    # rolling over is the earliest cycle-top warning.
    "weekly_drop_pct": -2.0,
    # WARN "cycle rollover": weekly change negative while the ~1-month
    # change is still above this (%) — the turn is fresh, not old news.
    "rollover_monthly_min_pct": 2.0,
    # Observations older than this many calendar days are flagged stale.
    "stale_after_days": 7,
}
REFRESH_INTERVALS["memory"] = 3600
CACHE_TTL["memory"] = 3600

# ---------------------------------------------------------------------------
# Published data snapshots (data/deploy separation)
#
# The refresher job (scripts/refresh_snapshots.py) force-pushes snapshot
# files as a single orphan commit to SNAPSHOT_BRANCH — never to a deployed
# code branch, because every push there triggers a container rebuild and
# hourly rebuilds pile up registry storage + egress cost. The app fetches
# the latest snapshots at runtime from SNAPSHOT_REMOTE_BASE instead.
# ---------------------------------------------------------------------------
SNAPSHOT_BRANCH = "snapshots"
SNAPSHOT_REMOTE_BASE = os.environ.get(
    "SNAPSHOT_REMOTE_BASE",
    "https://raw.githubusercontent.com/SunFish98/MacroDashboard/snapshots/",
)

# ---------------------------------------------------------------------------
# US Stock Market Holidays (NYSE / NASDAQ closures)
# ---------------------------------------------------------------------------
MARKET_HOLIDAYS = [
    # 2025
    {"date": "2025-01-01", "name_cn": "元旦",         "name_en": "New Year's Day"},
    {"date": "2025-01-20", "name_cn": "马丁·路德·金纪念日", "name_en": "MLK Day"},
    {"date": "2025-02-17", "name_cn": "总统日",        "name_en": "Presidents' Day"},
    {"date": "2025-04-18", "name_cn": "耶稣受难日",    "name_en": "Good Friday"},
    {"date": "2025-05-26", "name_cn": "阵亡将士纪念日","name_en": "Memorial Day"},
    {"date": "2025-06-19", "name_cn": "六月节",        "name_en": "Juneteenth"},
    {"date": "2025-07-04", "name_cn": "独立日",        "name_en": "Independence Day"},
    {"date": "2025-09-01", "name_cn": "劳动节",        "name_en": "Labor Day"},
    {"date": "2025-11-27", "name_cn": "感恩节",        "name_en": "Thanksgiving"},
    {"date": "2025-12-25", "name_cn": "圣诞节",        "name_en": "Christmas Day"},
    # 2026
    {"date": "2026-01-01", "name_cn": "元旦",         "name_en": "New Year's Day"},
    {"date": "2026-01-19", "name_cn": "马丁·路德·金纪念日", "name_en": "MLK Day"},
    {"date": "2026-02-16", "name_cn": "总统日",        "name_en": "Presidents' Day"},
    {"date": "2026-04-03", "name_cn": "耶稣受难日",    "name_en": "Good Friday"},
    {"date": "2026-05-25", "name_cn": "阵亡将士纪念日","name_en": "Memorial Day"},
    {"date": "2026-06-19", "name_cn": "六月节",        "name_en": "Juneteenth"},
    {"date": "2026-07-03", "name_cn": "独立日（补休）","name_en": "Independence Day (obs.)"},
    {"date": "2026-09-07", "name_cn": "劳动节",        "name_en": "Labor Day"},
    {"date": "2026-11-26", "name_cn": "感恩节",        "name_en": "Thanksgiving"},
    {"date": "2026-12-25", "name_cn": "圣诞节",        "name_en": "Christmas Day"},
]

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
FLASK_PORT = int(os.environ.get("PORT") or os.environ.get("FLASK_PORT", "5050"))
DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
