"""EOD Historical Data (EODHD) vendor: end-of-day OHLCV prices.

EODHD is wired as the *primary* vendor for ``core_stock_apis`` (with yfinance
as the configured fallback) because its end-of-day series finalizes the latest
completed session promptly and covers global exchanges — useful when the run
happens outside US market hours, where yfinance can still be serving a
not-yet-finalized (NaN-OHLC) bar for the current day.

Only the EOD price endpoint is integrated here: on the free EODHD plan
fundamentals/intraday are gated (HTTP 403), so those categories stay on
yfinance. The key is read from ``EODHD_API_KEY``; an empty key raises a
``VendorNotConfiguredError`` so the router cleanly falls back to the next
configured vendor instead of emitting prose the agent could hallucinate around.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
import requests

from .errors import VendorNotConfiguredError, VendorRateLimitError
from .symbol_utils import NoMarketDataError, normalize_symbol

logger = logging.getLogger(__name__)

API_BASE_URL = "https://eodhd.com/api"
# Network timeout (seconds) so a stalled request can't hang the agents.
REQUEST_TIMEOUT = 30


class EODHDNotConfiguredError(VendorNotConfiguredError):
    """Raised when EODHD is selected but no API key is configured."""


class EODHDRateLimitError(VendorRateLimitError):
    """Raised when the EODHD daily request quota is exhausted."""


def get_api_key() -> str:
    """Retrieve the EODHD API token from the environment."""
    import os

    api_key = os.getenv("EODHD_API_KEY")
    if not api_key:
        raise EODHDNotConfiguredError("EODHD_API_KEY environment variable is not set.")
    return api_key


def eodhd_symbol(symbol: str) -> str:
    """Map a user/Yahoo-style symbol to EODHD's ``CODE.EXCHANGE`` convention.

    EODHD identifies instruments as ``CODE.EXCHANGE`` (``AAPL.US``,
    ``0700.HK``, ``NDX.INDX``, ``BTC-USD.CC``, ``EURUSD.FOREX``). We first run
    the shared :func:`normalize_symbol` (which yields a Yahoo canonical such as
    ``^NDX`` or ``BTC-USD``), then translate the Yahoo shape to EODHD's:

        ^NDX        -> NDX.INDX     (index)
        BTC-USD     -> BTC-USD.CC   (crypto)
        EURUSD=X    -> EURUSD.FOREX (spot forex)
        0700.HK     -> 0700.HK      (already exchange-suffixed: unchanged)
        AAPL        -> AAPL.US      (bare equity defaults to US)

    Yahoo futures (``GC=F``) have no clean EODHD equivalent here; they are
    returned unchanged so the request fails and the router falls back.
    """
    canonical = normalize_symbol(symbol)

    # Already exchange-qualified (contains a '.', e.g. 0700.HK, BMW.XETRA).
    if "." in canonical and not canonical.startswith("^"):
        return canonical
    # Index symbols: Yahoo ^NDX -> EODHD NDX.INDX.
    if canonical.startswith("^"):
        return f"{canonical[1:]}.INDX"
    # Crypto: Yahoo BTC-USD -> EODHD BTC-USD.CC.
    if canonical.endswith("-USD"):
        return f"{canonical}.CC"
    # Spot forex: Yahoo EURUSD=X -> EODHD EURUSD.FOREX.
    if canonical.endswith("=X"):
        return f"{canonical[:-2]}.FOREX"
    # Futures (GC=F) and anything else: leave for the vendor to reject.
    if "=" in canonical:
        return canonical
    # Bare equity ticker defaults to the US exchange.
    return f"{canonical}.US"


def _eodhd_get(path: str, params: dict) -> list | dict:
    """Issue a GET to EODHD, classifying quota and config errors for the router."""
    api_params = {**params, "api_token": get_api_key(), "fmt": "json"}
    response = requests.get(f"{API_BASE_URL}/{path}", params=api_params, timeout=REQUEST_TIMEOUT)

    if response.status_code == 429:
        raise EODHDRateLimitError("EODHD daily request quota exceeded (HTTP 429).")
    # The free plan returns 403 with a plain-text body for gated endpoints; a
    # bad token also returns 401/403. Surface as "not configured" so the router
    # tries the next vendor rather than treating it as a hard failure.
    if response.status_code in (401, 402, 403):
        raise EODHDNotConfiguredError(
            f"EODHD request not permitted (HTTP {response.status_code}): "
            f"{response.text[:200]}"
        )
    response.raise_for_status()

    try:
        return response.json()
    except ValueError:
        # Non-JSON body (e.g. a plain-text plan-limit message) — not usable data.
        raise EODHDNotConfiguredError(
            f"EODHD returned a non-JSON body: {response.text[:200]}"
        ) from None


def _eod_dataframe(symbol: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, str]:
    """Fetch EODHD EOD rows as a normalized capitalized-OHLCV DataFrame.

    Returns ``(df, eod_symbol)`` with columns
    ``Date, Open, High, Low, Close, Adj Close, Volume`` (subset as available),
    date-sorted ascending. Raises ``NoMarketDataError`` on an empty/unusable
    response so the router falls back to the next vendor.
    """
    eod_sym = eodhd_symbol(symbol)
    # EODHD's date range is inclusive on both ends.
    data = _eodhd_get(
        f"eod/{eod_sym}",
        {"from": start_date, "to": end_date, "period": "d", "order": "a"},
    )
    if not isinstance(data, list) or not data:
        raise NoMarketDataError(
            symbol, eod_sym, f"EODHD returned no rows between {start_date} and {end_date}"
        )

    df = pd.DataFrame(data)
    if "date" not in df.columns or "close" not in df.columns:
        raise NoMarketDataError(symbol, eod_sym, "EODHD response missing OHLCV columns")

    df = df.rename(
        columns={
            "date": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "adjusted_close": "Adj Close",
            "volume": "Volume",
        }
    )
    cols = [c for c in ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"] if c in df.columns]
    df = df[cols].sort_values("Date")
    for col in ("Open", "High", "Low", "Close", "Adj Close"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").round(2)
    return df, eod_sym


def get_stock(symbol: str, start_date: str, end_date: str) -> str:
    """Return daily OHLCV for ``symbol`` over ``[start_date, end_date]`` as CSV.

    Output mirrors the yfinance/Alpha Vantage price formatting (a header block
    plus a ``Date,Open,High,Low,Close,Adj Close,Volume`` CSV) so the downstream
    report builders treat every vendor identically.
    """
    df, eod_sym = _eod_dataframe(symbol, start_date, end_date)
    csv_string = df.to_csv(index=False)
    label = eod_sym if eod_sym == symbol.upper() else f"{eod_sym} (from {symbol})"
    header = (
        f"# Stock data for {label} from {start_date} to {end_date} (source: EODHD)\n"
        f"# Total records: {len(df)}\n"
        f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    )
    return header + csv_string


def get_ohlcv_dataframe(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Return EODHD OHLCV as a stockstats-ready DataFrame.

    Columns: ``Date`` (datetime), ``Open, High, Low, Close, Volume``. Used by
    ``stockstats_utils.load_ohlcv`` as the primary history source for the
    verification snapshot and indicators, so they reflect the latest finalized
    session even when yfinance returns a NaN-OHLC bar for it.
    """
    df, _ = _eod_dataframe(symbol, start_date, end_date)
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    keep = [c for c in ["Date", "Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    return df[keep]
