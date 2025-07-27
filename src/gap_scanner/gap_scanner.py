from __future__ import annotations
import datetime as _dt
import os
from typing import List, Dict
import time
import logging
from pathlib import Path


from tinkoff.invest import Client, CandleInterval, InstrumentIdType
from tinkoff.invest.schemas import AssetsRequest
from tinkoff.invest.utils import now

from dotenv import load_dotenv
from tinkoff.invest.exceptions import RequestError
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware



__all__ = ["scan_gap_up"]

# ---------------------------------------------------------------------------
# Load environment variables from .env
# ---------------------------------------------------------------------------
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)  # load variables from .env into environment
else:
    raise RuntimeError(".env file not found. Please create a .env file with TINKOFF_INVEST_TOKEN=<your token>")


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
_log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
_log_level = getattr(logging, _log_level_str, logging.INFO)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
# Downgrade noisy third‑party loggers (e.g. Tinkoff gRPC stubs) to DEBUG only
_noisy_libs = ("tinkoff", "grpc")
for _lib in _noisy_libs:
    lib_logger = logging.getLogger(_lib)
    # Show their logs only when global level is DEBUG, otherwise silence them
    lib_logger.setLevel(logging.DEBUG if _log_level == logging.DEBUG else logging.WARNING)
logger = logging.getLogger(__name__)
# library calls like "GetAssets"/"GetCandles" will appear only with LOG_LEVEL=DEBUG

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(title="Gap Scanner API")

# Allow requests from any origin (adjust in production!)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _prev_trading_day(day: _dt.date) -> _dt.date:
    """Return the previous weekday (very rough market calendar)."""
    prev = day - _dt.timedelta(days=1)
    return prev


def _to_ts(date_: _dt.date) -> _dt.datetime:
    """Convert date to UTC midnight timestamp (Tinkoff expects UTC)."""
    return _dt.datetime.combine(date_, _dt.time.min, tzinfo=_dt.timezone.utc)


# ---------------------------------------------------------------------------
# Core scanner
# ---------------------------------------------------------------------------

def scan_gap_up(*, min_gap: float = 0.10, date: _dt.date | None = None) -> List[Dict]:
    """Scan all shares via GetAssets and return those with an **opening gap ≥ min_gap**.

    Parameters
    ----------
    min_gap: float, default 0.10
        Minimal gap expressed as fraction (0.10 == 10 %).
    date: datetime.date | None
        Market date to check. Defaults to **today** in UTC.

    Returns
    -------
    List[dict]
        Each dict contains keys: ticker, figi, uid, prev_close, open, gap.
    """
    token = os.getenv("TINKOFF_INVEST_TOKEN")
    if not token:
        raise RuntimeError("Environment variable TINKOFF_INVEST_TOKEN is not set")

    if date is None:
        date = now().date()
    prev_day = _prev_trading_day(date)

    result: List[Dict] = []
    max_gap: float = float("-inf")
    max_stock: Dict | None = None

    with Client(token) as client:
        assets_resp = client.instruments.get_assets(AssetsRequest())

        total = len(assets_resp.assets)
        for i, asset in enumerate(assets_resp.assets):
            # keep only security‑type assets
            if getattr(asset.type, "name", str(asset.type)) != "ASSET_TYPE_SECURITY":
                continue
            if not getattr(asset, "instruments", None):
                continue  # nothing to inspect

            # every Asset can contain multiple instruments (different exchanges, classes, etc.)
            for instr in asset.instruments:
                if instr.instrument_type != "share":
                    continue  # we only care about shares

                uid = instr.uid
                figi = instr.figi
                ticker = instr.ticker

                # --- previous close candle (daily) ---
                prev_from = _to_ts(prev_day)
                prev_to = _to_ts(prev_day + _dt.timedelta(days=1))
                try:
                    prev_candles = client.market_data.get_candles(
                        instrument_id=uid,
                        from_=prev_from,
                        to=prev_to,
                        interval=CandleInterval.CANDLE_INTERVAL_DAY,
                    ).candles
                except RequestError as e:
                    if "RESOURCE_EXHAUSTED" in str(e):
                        logger.warning("rate‑limit hit (prev candles); sleeping 60 s then retrying once")
                        time.sleep(60)
                        try:
                            prev_candles = client.market_data.get_candles(
                                instrument_id=uid,
                                from_=prev_from,
                                to=prev_to,
                                interval=CandleInterval.CANDLE_INTERVAL_DAY,
                            ).candles
                        except RequestError:
                            logger.error(f"retry failed for {ticker}; skipping")
                            continue
                    else:
                        logger.error(f"{ticker}: {e}")
                        continue
                if not prev_candles:
                    continue

                # --- today open candle (daily) ---
                today_from = _to_ts(date)
                today_to = _to_ts(date + _dt.timedelta(days=1))
                try:
                    today_candles = client.market_data.get_candles(
                        instrument_id=uid,
                        from_=today_from,
                        to=today_to,
                        interval=CandleInterval.CANDLE_INTERVAL_DAY,
                    ).candles
                except RequestError as e:
                    if "RESOURCE_EXHAUSTED" in str(e):
                        logger.warning("rate‑limit hit (today candles); sleeping 60 s then retrying once")
                        time.sleep(60)
                        try:
                            today_candles = client.market_data.get_candles(
                                instrument_id=uid,
                                from_=today_from,
                                to=today_to,
                                interval=CandleInterval.CANDLE_INTERVAL_DAY,
                            ).candles
                        except RequestError:
                            logger.error(f"retry failed for {ticker}; skipping")
                            continue
                    else:
                        logger.error(f"{ticker}: {e}")
                        continue
                if not today_candles:
                    continue

                prev_close = prev_candles[-1].close
                prev_close_price = prev_close.units + prev_close.nano / 1e9

                today_open = today_candles[0].open  # first candle of the day
                today_open_price = today_open.units + today_open.nano / 1e9

                gap = (today_open_price - prev_close_price) / prev_close_price
                logger.info(f'{i}/{total}"ticker": {ticker}'
                             f' "figi": {figi}'
                             f' "uid": {uid}'
                             f' "prev_close": {prev_close_price}'
                             f' "open": {today_open_price}'
                             f' "gap": {gap}')
                # track absolute maximum gap stock
                if gap > max_gap:
                    max_gap = gap
                    max_stock = {
                        "ticker": ticker,
                        "figi": figi,
                        "uid": uid,
                        "prev_close": prev_close_price,
                        "open": today_open_price,
                        "gap": gap,
                    }
                if gap >= min_gap:
                    result.append(
                        {
                            "ticker": ticker,
                            "figi": figi,
                            "uid": uid,
                            "prev_close": prev_close_price,
                            "open": today_open_price,
                            "gap": gap,
                        }
                    )

    # If no stocks met the gap threshold, include the max‑gap stock for testing
    if not result and max_stock is not None:
        logger.info(
            f"No gaps ≥{min_gap*100:.2f}% found; adding max‑gap "
            f"{max_stock['ticker']} ({max_gap*100:.2f}%) for back‑test"
        )
        result.append(max_stock)
    # Sort by gap desc
    return sorted(result, key=lambda x: x["gap"], reverse=True)



# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/gap-up")
def read_gap_up(
    min_gap: float = Query(0.10, description="Minimal gap fraction (0.10 == 10 %)", ge=0.0),
    date: str | None = Query(None, description="Date in YYYY-MM-DD; default today (UTC)"),
):
    """Return shares whose **opening gap** ≥ *min_gap* on the given *date*."""
    if date:
        try:
            date_parsed = _dt.datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format, expected YYYY-MM-DD")
    else:
        date_parsed = None

    results = scan_gap_up(min_gap=min_gap, date=date_parsed)
    # Mark whether this record was returned only for back‑test purposes (no gap met the threshold)
    for r in results:
        r["gap_test"] = r["gap"] < min_gap
    return {"count": len(results), "results": results}
