import os
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from tinkoff.invest import Client, CandleInterval, HistoricCandle, Quotation
from dotenv import load_dotenv

def _quote_to_float(q: "Quotation") -> float:
    """Convert a Tinkoff Quotation object to float (for price fields)."""
    return q.units + q.nano / 1e9

# Load API token from .env
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)
else:
    raise RuntimeError(".env file not found. Please create a .env file with TINKOFF_INVEST_TOKEN=<your token>")

TOKEN = os.getenv("TINKOFF_INVEST_TOKEN")
if not TOKEN:
    raise RuntimeError("TINKOFF_INVEST_TOKEN is not set in environment")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# Initialize FastAPI app for the Bull Flag strategy
app = FastAPI(
    title="Bull Flag Pattern API",
    description=(
        "Bull Flag strategy microservice: identifies Bull Flag pattern occurrences "
        "for a given ticker on the specified date (analyzing 1-minute and 5-minute intervals, "
        "as recommended by Ross Cameron)."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic models for response
class BullFlagResult(BaseModel):
    triggered: bool
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None  # profit target equal to flagpole height added to entry
    trigger_time: datetime | None = None

class BullFlagResponse(BaseModel):
    ticker: str
    date: date
    bull_flag_1min: BullFlagResult
    bull_flag_5min: BullFlagResult

def _fetch_candles(uid: str, trade_date: date, interval: CandleInterval) -> list[HistoricCandle]:
    """Fetch historical candles for the given date and interval using Tinkoff API."""
    start_ts = datetime.combine(trade_date, datetime.min.time(), tzinfo=timezone.utc)
    end_ts = start_ts + timedelta(days=1)
    try:
        with Client(TOKEN) as client:
            candles = client.market_data.get_candles(
                instrument_id=uid,
                from_=start_ts,
                to=end_ts,
                interval=interval,
            ).candles
    except Exception as exc:
        logging.exception("Tinkoff Invest API error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not candles:
        raise HTTPException(status_code=500, detail=f"No candles for {trade_date} with interval {interval.name}")
    return candles

def _analyze_bull_flag(candles: list[HistoricCandle]) -> BullFlagResult:
    """Analyze the Bull Flag pattern in a series of candles. Returns BullFlagResult."""
    triggered = False
    entry_price = None
    stop_price = None
    target_price = None
    trigger_time = None

    # Prepare lists of highs, lows, volumes for analysis
    highs = [_quote_to_float(c.high) for c in candles]
    lows  = [_quote_to_float(c.low) for c in candles]
    volumes = [c.volume for c in candles]  # volume is directly available from HistoricCandle

    n = len(candles)
    # Scan through the candles to find a bull flag setup
    for i in range(2, n - 2):
        # Identify a local peak (potential flagpole top) at index i
        if highs[i] <= highs[i-1] or highs[i] <= highs[i+1]:
            continue  # not a local high if previous or next candle has a higher or equal high
        # Ensure at least two subsequent candles form a downward/sideways consolidation (lower highs)
        if highs[i+1] < highs[i] and highs[i+2] < highs[i+1]:
            peak_index = i
            peak_price = highs[i]
            # Find the end of the consolidation (first candle where high >= peak_price)
            j = i + 1
            lowest_low = peak_price  # track lowest price in the flag consolidation
            while j < n and highs[j] <= peak_price:
                if lows[j] < lowest_low:
                    lowest_low = lows[j]
                j += 1
            # Now j is the first index where price attempted to break above the peak (or end of data)
            if j < n and highs[j] > peak_price:
                # Check for volume spike on the breakout candle j
                cons_volumes = volumes[i+1:j] if j > i+1 else [volumes[i+1]]
                avg_cons_vol = sum(cons_volumes) / len(cons_volumes) if cons_volumes else 0
                if avg_cons_vol == 0:
                    avg_cons_vol = 1  # avoid division by zero
                if volumes[j] < 2 * avg_cons_vol:
                    # Volume on breakout is less than 2× the consolidation average – likely a false breakout
                    logging.info(
                        f"Bull Flag breakout at {candles[j].time.isoformat()} not confirmed due to low volume "
                        f"(vol={volumes[j]}, avg_cons_vol={avg_cons_vol:.1f})"
                    )
                    continue
                # Valid Bull Flag breakout confirmed
                triggered = True
                entry_price = peak_price
                stop_price = lowest_low
                # Calculate profit target = entry + flagpole height
                start_window = max(0, peak_index - 10)  # look ~10 candles back for flagpole start
                flagpole_low = min(lows[start_window: peak_index + 1])
                flagpole_height = peak_price - flagpole_low
                target_price = peak_price + flagpole_height
                trigger_time = candles[j].time
                logging.info(
                    f"Bull Flag TRIGGERED at {trigger_time.isoformat()} – entry={entry_price:.2f}, "
                    f"stop={stop_price:.2f}, target={target_price:.2f}"
                )
                break  # stop after the first bull flag pattern found

    return BullFlagResult(
        triggered=triggered,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        trigger_time=trigger_time,
    )

@app.get("/bull-flag", response_model=BullFlagResponse)
def bull_flag(
    ticker: str = Query(..., description="Ticker symbol, e.g. SBER"),
    uid: str    = Query(..., description="Instrument UID in Tinkoff Invest API"),
    date_: str | None = Query(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Trade date in YYYY-MM-DD format (UTC). Defaults to today.",
    ),
):
    """Analyze Bull Flag pattern for the given ticker on the specified date (UTC)."""
    try:
        trade_date = datetime.strptime(date_, "%Y-%m-%d").date() if date_ else datetime.utcnow().date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date format") from exc

    # Fetch full-day 1-minute and 5-minute candles for the trade_date
    candles_1m = _fetch_candles(uid, trade_date, CandleInterval.CANDLE_INTERVAL_1_MIN)
    candles_5m = _fetch_candles(uid, trade_date, CandleInterval.CANDLE_INTERVAL_5_MIN)
    logging.info(
        "Fetched %d candles (1min) and %d candles (5min) for %s on %s",
        len(candles_1m), len(candles_5m), ticker, trade_date
    )

    # Analyze Bull Flag pattern on both timeframes
    result_1m = _analyze_bull_flag(candles_1m)
    result_5m = _analyze_bull_flag(candles_5m)
    return BullFlagResponse(
        ticker=ticker,
        date=trade_date,
        bull_flag_1min=result_1m,
        bull_flag_5min=result_5m,
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "bull_flag:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8004)),
        reload=False,
    )
