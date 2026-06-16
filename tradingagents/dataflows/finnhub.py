"""Finnhub vendor: company news, market news, and insider data.

Finnhub is wired as the *primary* vendor for ``news_data`` (with yfinance as
the configured fallback). Beyond the news/insider-transaction methods the
framework already had, this module adds an **insider-sentiment** capability
(Finnhub's ``/stock/insider-sentiment``: monthly net insider share change and
MSPR), which fills the paper's "insider sentiment" data category that had no
implementation before.

The key is read from ``FINNHUB_API_KEY``; an empty key raises a
``VendorNotConfiguredError`` so the router cleanly falls back to the next
configured vendor. Finnhub's free tier covers US equities; non-US symbols may
return empty, in which case a typed no-data error lets the router fall back.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import requests

from .errors import NoMarketDataError, VendorNotConfiguredError, VendorRateLimitError

logger = logging.getLogger(__name__)

API_BASE_URL = "https://finnhub.io/api/v1"
# Network timeout (seconds) so a stalled request can't hang the agents.
REQUEST_TIMEOUT = 30
# Cap article counts so a single tool call can't flood the agent's context.
MAX_ARTICLES = 30


class FinnhubNotConfiguredError(VendorNotConfiguredError):
    """Raised when Finnhub is selected but no API key is configured."""


class FinnhubRateLimitError(VendorRateLimitError):
    """Raised when the Finnhub rate limit is exceeded."""


def get_api_key() -> str:
    """Retrieve the Finnhub API key from the environment."""
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        raise FinnhubNotConfiguredError("FINNHUB_API_KEY environment variable is not set.")
    return api_key


def _finnhub_symbol(symbol: str) -> str:
    """Finnhub uses bare upper-cased tickers; drop a Yahoo index caret."""
    return symbol.strip().upper().lstrip("^")


def _finnhub_get(path: str, params: dict) -> list | dict:
    """Issue a GET to Finnhub, classifying rate-limit and config errors."""
    api_params = {**params, "token": get_api_key()}
    response = requests.get(f"{API_BASE_URL}/{path}", params=api_params, timeout=REQUEST_TIMEOUT)

    if response.status_code == 429:
        raise FinnhubRateLimitError("Finnhub rate limit exceeded (HTTP 429).")
    if response.status_code in (401, 403):
        raise FinnhubNotConfiguredError(
            f"Finnhub request not permitted (HTTP {response.status_code}); check FINNHUB_API_KEY."
        )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        raise NoMarketDataError(path, None, "Finnhub returned a non-JSON body") from None


def _fmt_unix(ts) -> str:
    """Format a Finnhub unix-second timestamp as a date string."""
    try:
        return datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return "unknown date"


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Company news for ``ticker`` over ``[start_date, end_date]`` (Finnhub)."""
    sym = _finnhub_symbol(ticker)
    data = _finnhub_get(
        "company-news", {"symbol": sym, "from": start_date, "to": end_date}
    )
    if not isinstance(data, list) or not data:
        raise NoMarketDataError(
            ticker, sym, f"Finnhub returned no company news between {start_date} and {end_date}"
        )

    # Most-recent first, capped.
    data = sorted(data, key=lambda a: a.get("datetime", 0), reverse=True)[:MAX_ARTICLES]
    lines = [
        f"# Company news for {sym} from {start_date} to {end_date} (source: Finnhub)",
        f"# Total articles: {len(data)}",
        "",
    ]
    for a in data:
        headline = (a.get("headline") or "").strip()
        summary = (a.get("summary") or "").strip()
        lines.append(f"## {_fmt_unix(a.get('datetime'))} — {a.get('source', 'Finnhub')}")
        lines.append(headline)
        if summary:
            lines.append(summary)
        if a.get("url"):
            lines.append(f"({a['url']})")
        lines.append("")
    return "\n".join(lines)


def get_global_news(curr_date: str, look_back_days: int = 7, limit: int = 50) -> str:
    """General market news (Finnhub ``/news?category=general``).

    Finnhub's general-news feed is not date-parameterized, so ``look_back_days``
    is applied as a client-side filter on each article's timestamp.
    """
    look_back_days = look_back_days or 7
    limit = limit or 50
    cutoff = datetime.strptime(curr_date, "%Y-%m-%d") - timedelta(days=look_back_days)
    data = _finnhub_get("news", {"category": "general"})
    if not isinstance(data, list) or not data:
        raise NoMarketDataError("general", None, "Finnhub returned no general market news")

    fresh = [a for a in data if datetime.utcfromtimestamp(int(a.get("datetime", 0))) >= cutoff]
    fresh = sorted(fresh, key=lambda a: a.get("datetime", 0), reverse=True)[:limit]
    if not fresh:
        # Fall back to the newest available if nothing is inside the window.
        fresh = sorted(data, key=lambda a: a.get("datetime", 0), reverse=True)[:limit]

    lines = [
        f"# Global market news as of {curr_date} (last {look_back_days} days, source: Finnhub)",
        f"# Total articles: {len(fresh)}",
        "",
    ]
    for a in fresh:
        lines.append(f"## {_fmt_unix(a.get('datetime'))} — {a.get('source', 'Finnhub')}")
        lines.append((a.get("headline") or "").strip())
        summary = (a.get("summary") or "").strip()
        if summary:
            lines.append(summary)
        lines.append("")
    return "\n".join(lines)


def get_insider_transactions(symbol: str) -> str:
    """Recent insider transactions (Finnhub ``/stock/insider-transactions``)."""
    sym = _finnhub_symbol(symbol)
    payload = _finnhub_get("stock/insider-transactions", {"symbol": sym})
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not rows:
        # Empty is normal for many valid symbols — report plainly, don't error.
        return f"No insider transactions reported by Finnhub for '{sym}'."

    rows = sorted(rows, key=lambda r: r.get("filingDate", ""), reverse=True)[:MAX_ARTICLES]
    lines = [
        f"# Insider transactions for {sym} (source: Finnhub)",
        f"# Total records shown: {len(rows)}",
        "",
        "| Filing date | Transaction date | Insider | Shares Δ | Holding after | Price | Code |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r.get('filingDate', '')} | {r.get('transactionDate', '')} "
            f"| {r.get('name', '')} | {r.get('change', '')} | {r.get('share', '')} "
            f"| {r.get('transactionPrice', '')} | {r.get('transactionCode', '')} |"
        )
    return "\n".join(lines)


def get_insider_sentiment(symbol: str, curr_date: str | None = None, look_back_days: int | None = None) -> str:
    """Monthly insider sentiment (Finnhub ``/stock/insider-sentiment``).

    Returns monthly net insider share change and MSPR (Monthly Share Purchase
    Ratio: positive = net insider buying, negative = net selling). This is the
    aggregated insider-*sentiment* signal the paper references (SEDI-style),
    distinct from raw transaction rows.
    """
    sym = _finnhub_symbol(symbol)
    # Default to a trailing one-year window ending on curr_date (or today).
    end_dt = datetime.strptime(curr_date, "%Y-%m-%d") if curr_date else datetime.utcnow()
    start_dt = end_dt - timedelta(days=look_back_days or 365)
    payload = _finnhub_get(
        "stock/insider-sentiment",
        {"symbol": sym, "from": start_dt.strftime("%Y-%m-%d"), "to": end_dt.strftime("%Y-%m-%d")},
    )
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not rows:
        return f"No insider-sentiment data reported by Finnhub for '{sym}'."

    rows = sorted(rows, key=lambda r: (r.get("year", 0), r.get("month", 0)))
    lines = [
        f"# Insider sentiment for {sym} (source: Finnhub)",
        "# MSPR: Monthly Share Purchase Ratio (-100..100); >0 net buying, <0 net selling.",
        "",
        "| Year | Month | Net share change | MSPR |",
        "|---:|---:|---:|---:|",
    ]
    for r in rows:
        mspr = r.get("mspr")
        mspr_str = f"{mspr:.2f}" if isinstance(mspr, (int, float)) else str(mspr)
        lines.append(f"| {r.get('year', '')} | {r.get('month', '')} | {r.get('change', '')} | {mspr_str} |")
    return "\n".join(lines)


def get_earnings(symbol: str, curr_date: str | None = None, look_back_days: int | None = None) -> str:
    """Earnings surprises, the next scheduled report, and the analyst trend (Finnhub).

    Aggregates three Finnhub endpoints into one report: historical EPS
    actual-vs-estimate surprises (``/stock/earnings``), the next scheduled
    earnings date with EPS/revenue estimates (``/calendar/earnings``), and the
    latest analyst recommendation distribution (``/stock/recommendation``).
    This fills the paper's "earnings reports" data category, which the statement
    tools (balance sheet / cash flow / income) did not cover. ``look_back_days``
    is accepted for signature parity with the other dated tools; the endpoints
    return their own recent windows.
    """
    sym = _finnhub_symbol(symbol)
    end_dt = datetime.strptime(curr_date, "%Y-%m-%d") if curr_date else datetime.utcnow()

    # The surprises call runs first and unguarded so a missing key / rate limit
    # surfaces to the router; the supplementary sections degrade quietly.
    surprises = _finnhub_get("stock/earnings", {"symbol": sym})
    lines = [f"# Earnings & analyst view for {sym} (source: Finnhub)", ""]

    lines.append("## Recent earnings surprises (EPS: actual vs estimate)")
    if isinstance(surprises, list) and surprises:
        lines += [
            "",
            "| Period | Qtr | Actual | Estimate | Surprise | Surprise % |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for r in surprises[:8]:
            sp = r.get("surprisePercent")
            sp_str = f"{sp:.2f}%" if isinstance(sp, (int, float)) else str(sp)
            lines.append(
                f"| {r.get('period', '')} | {r.get('quarter', '')} | {r.get('actual', '')} "
                f"| {r.get('estimate', '')} | {r.get('surprise', '')} | {sp_str} |"
            )
    else:
        lines.append("No earnings-surprise history reported.")

    lines += ["", "## Next scheduled earnings"]
    try:
        cal = _finnhub_get(
            "calendar/earnings",
            {
                "symbol": sym,
                "from": end_dt.strftime("%Y-%m-%d"),
                "to": (end_dt + timedelta(days=120)).strftime("%Y-%m-%d"),
            },
        )
        upcoming = cal.get("earningsCalendar", []) if isinstance(cal, dict) else []
        if upcoming:
            n = upcoming[0]
            lines.append(
                f"- {n.get('date', '?')} ({n.get('hour', '')}), "
                f"Q{n.get('quarter', '')} {n.get('year', '')}: "
                f"EPS est {n.get('epsEstimate')}, revenue est {n.get('revenueEstimate')}"
            )
        else:
            lines.append("- No upcoming earnings date in the next ~120 days.")
    except Exception as exc:  # noqa: BLE001 — supplementary section degrades gracefully
        lines.append(f"- (earnings calendar unavailable: {type(exc).__name__})")

    lines += ["", "## Analyst recommendation trend (latest period)"]
    try:
        recs = _finnhub_get("stock/recommendation", {"symbol": sym})
        if isinstance(recs, list) and recs:
            r = recs[0]
            lines.append(
                f"- As of {r.get('period', '')}: Strong Buy {r.get('strongBuy', 0)}, "
                f"Buy {r.get('buy', 0)}, Hold {r.get('hold', 0)}, "
                f"Sell {r.get('sell', 0)}, Strong Sell {r.get('strongSell', 0)}"
            )
        else:
            lines.append("- No analyst recommendation data.")
    except Exception as exc:  # noqa: BLE001 — supplementary section degrades gracefully
        lines.append(f"- (recommendation trend unavailable: {type(exc).__name__})")

    return "\n".join(lines)
