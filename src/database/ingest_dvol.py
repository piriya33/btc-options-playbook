"""
DVOL ingestion — two modes:

1. Automatic (async): backfill_dvol_30d() on bot startup, then
   ingest_yesterday_dvol() daily at 00:30 UTC via job_queue.

2. Manual CSV: ingest_csv(filepath) — expects columns: time (unix s), close.
   Useful for seeding historical data from TradingView exports.

DVOL data always comes from Deribit mainnet (public endpoint) regardless of
whether the bot is running against testnet or mainnet.
"""

import csv
import logging
import httpx
from datetime import datetime, UTC

from database.queries import upsert_dvol_candles, get_latest_dvol_date

logger = logging.getLogger(__name__)

_DVOL_URL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"


# ── Async Deribit fetch ────────────────────────────────────────────────────────

async def _fetch_candles(start_ms: int, end_ms: int) -> list:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(_DVOL_URL, params={
            "currency":        "BTC",
            "resolution":      "1D",
            "start_timestamp": start_ms,
            "end_timestamp":   end_ms,
        })
        r.raise_for_status()
    return r.json().get("result", {}).get("data", [])


async def backfill_dvol_30d() -> int:
    """
    Fetch and upsert the last 32 days of daily DVOL candles from Deribit mainnet.
    Called at bot startup when the DB has no data or data is stale.
    Returns the number of candles written.
    """
    now_ms   = int(datetime.now(UTC).timestamp() * 1000)
    start_ms = now_ms - 86_400_000 * 32   # 32 days back for headroom
    candles  = await _fetch_candles(start_ms, now_ms)
    if candles:
        upsert_dvol_candles(candles)
        logger.info(f"DVOL backfill: {len(candles)} candles written")
    return len(candles)


async def ingest_yesterday_dvol(context=None):
    """
    Fetch the last 2 days of DVOL and upsert — run daily at 00:30 UTC.
    The 2-day window ensures yesterday's candle is always captured even if
    Deribit's daily candle finalises slightly after midnight.
    """
    now_ms   = int(datetime.now(UTC).timestamp() * 1000)
    start_ms = now_ms - 86_400_000 * 2
    try:
        candles = await _fetch_candles(start_ms, now_ms)
        if candles:
            upsert_dvol_candles(candles)
            logger.info(f"DVOL daily ingest: {len(candles)} candle(s) upserted")
    except Exception as e:
        logger.error(f"DVOL daily ingest failed: {e}")


# ── Manual CSV import ─────────────────────────────────────────────────────────

def ingest_csv(filepath: str) -> int:
    """
    Import DVOL history from a TradingView CSV export.
    Expected columns: time (Unix timestamp in seconds), close.
    Returns the number of rows written.
    """
    candles = []
    try:
        with open(filepath, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts_s  = int(row["time"])
                    close = float(row["close"])
                    # Convert to ms and build a synthetic [ts_ms, _, _, _, close] tuple
                    candles.append([ts_s * 1000, close, close, close, close])
                except Exception as e:
                    logger.warning(f"Skipping CSV row: {e}")
    except Exception as e:
        logger.error(f"Cannot read CSV '{filepath}': {e}")
        return 0

    if candles:
        upsert_dvol_candles(candles)
        logger.info(f"DVOL CSV import: {len(candles)} rows written from {filepath}")
    return len(candles)


# ── Startup helper ─────────────────────────────────────────────────────────────

async def ensure_dvol_history() -> str:
    """
    Called at bot startup. Backfills 30d if the DB has no data or is >1 day stale.
    Returns a human-readable status string for the startup log.
    """
    latest = get_latest_dvol_date()
    if latest is None:
        count = await backfill_dvol_30d()
        return f"DVOL DB was empty — backfilled {count} days"

    days_stale = (datetime.now(UTC).replace(tzinfo=None) - latest).days
    if days_stale > 1:
        count = await backfill_dvol_30d()
        return f"DVOL was {days_stale}d stale — backfilled {count} days"

    return f"DVOL up to date (latest: {latest.date()})"
