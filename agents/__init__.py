"""Package init: pin yfinance's tz cache to a repo-local, always-writable dir.

Under launchd the process inherits no HOME, so yfinance's default cache path
(~/Library/Caches/py-yfinance) can't be resolved/created and its sqlite tz cache
raises `OperationalError('unable to open database file')`. On 2026-06-22 that
cascaded into "possibly delisted / no price data" failures across the universe
and a degraded data window. Anchoring the cache inside the repo removes the HOME
dependency entirely — the dir is always present and writable regardless of how
the process is launched (launchd, cron, shell).
"""
from pathlib import Path

try:  # never let cache setup break an import
    import yfinance as _yf

    _CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "py-yfinance"
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _yf.set_tz_cache_location(str(_CACHE_DIR))
except Exception:
    pass
