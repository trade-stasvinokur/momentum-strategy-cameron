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

# Load API token from .env (in this strategy's folder)
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

# Initialize FastAPI app for the ABCD strategy
app = FastAPI(
    title="ABCD Pattern Strategy API",
    description=(
        "ABCD pattern strategy microservice: identifies the ABCD pattern for a given ticker on the specified date. "
        "Analyzes 1-minute and 5-minute candlestick data (as used in momentum trading by Ross Cameron)."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic models for the pattern result and response
class ABCDResult(BaseModel):
    triggered: bool
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    trigger_time: datetime | None = None

class ABCDResponse(BaseModel):
    ticker: str
    date: date
    abcd_1min: ABCDResult
    abcd_5min: ABCDResult

def _fetch_candles(uid: str, trade_date: date, interval: CandleInterval) -> list[HistoricCandle]:
    """Fetch historical candles for the given date and interval using Tinkoff Invest API."""
    # Define the UTC start and end timestamps for the trading date (midnight to midnight)
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
        raise HTTPException(
            status_code=500, 
            detail=f"No candles for {trade_date} with interval {interval.name}"
        )
    return candles

def _analyze_abcd(candles: list[HistoricCandle]) -> ABCDResult:
    """Analyze the candle series for an ABCD pattern. Returns an ABCDResult."""
    triggered = False
    entry_price = None
    stop_price = None
    target_price = None
    trigger_time = None

    n = len(candles)
    if n < 4:
        return ABCDResult(triggered=False)  # Not enough data to form pattern

    # Prepare lists of highs, lows, volumes for easier analysis
    highs = [_quote_to_float(c.high) for c in candles]
    lows  = [_quote_to_float(c.low) for c in candles]
    volumes = [c.volume for c in candles]

    # Loop through candles to find ABCD pattern
    for i in range(2, n - 2):
        # Identify a local peak at index i (potential point B)
        if highs[i] <= highs[i-1] or highs[i] <= highs[i+1]:
            continue  # i is not a local high if neighbors are equal or higher
        # Ensure at least two subsequent candles indicate a pullback (lower highs)
        if highs[i+1] < highs[i] and highs[i+2] < highs[i+1]:
            peak_index = i
            peak_price = highs[i]      # Price at point B
            # Find the pullback after B: traverse forward until breakout above B
            j = i + 1
            lowest_low = peak_price    # track lowest price during pullback (point C)
            while j < n and highs[j] <= peak_price:
                # Update lowest point in the pullback
                if lows[j] < lowest_low:
                    lowest_low = lows[j]
                j += 1
            # Now, j is the first index where price breaks above peak_price (potential D)
            if j < n and highs[j] > peak_price:
                # We have A-B-C-D candidates: A somewhere before i, B at i, C's low = lowest_low, breakout at j
                # Determine point A's price as the lowest price leading up to B (within a reasonable window)
                start_window = max(0, peak_index - 10)
                A_price = min(lows[start_window : peak_index + 1])
                # Condition 1: C must be above A (pullback low > A's price)
                if lowest_low <= A_price:
                    continue  # pullback went too deep (not above A)
                # Condition 2: Retracement between 20% and 80% of A→B
                impulse_height = peak_price - A_price
                if impulse_height <= 0:
                    continue  # no upward impulse
                retrace = peak_price - lowest_low
                retrace_ratio = retrace / impulse_height
                if retrace_ratio < 0.2 or retrace_ratio > 0.8:
                    continue  # pullback not in the 20-80% range
                # Condition 3: Volume drop on pullback and volume rise on breakout
                cons_volumes = volumes[i+1 : j] if j > i+1 else [volumes[i+1]]
                # Check volume decline during pullback: max pullback volume < peak volume of initial move
                peak_vol_impulse = max(volumes[start_window : peak_index + 1])  # max vol from A to B
                if cons_volumes and max(cons_volumes) >= peak_vol_impulse:
                    continue  # volume during pullback did not decline sufficiently
                # Check volume surge on breakout: breakout volume >= 2× average pullback volume
                breakout_vol = volumes[j]
                avg_cons_vol = sum(cons_volumes) / len(cons_volumes) if cons_volumes else 0
                if avg_cons_vol == 0:
                    avg_cons_vol = 1  # avoid division by zero
                if breakout_vol < 2 * avg_cons_vol:
                    logging.info(
                        f"ABCD breakout at {candles[j].time.isoformat()} not confirmed due to low volume "
                        f"(vol={breakout_vol}, avg_pullback_vol={avg_cons_vol:.1f})"
                    )
                    continue
                # All conditions met – ABCD pattern confirmed
                triggered = True
                entry_price = peak_price
                stop_price = lowest_low
                target_price = peak_price + impulse_height  # profit target: add initial impulse height to B
                trigger_time = candles[j].time
                logging.info(
                    f"ABCD PATTERN TRIGGERED at {trigger_time.isoformat()} – "
                    f"entry={entry_price:.2f}, stop={stop_price:.2f}, target={target_price:.2f}"
                )
                break  # exit after first pattern found
    return ABCDResult(
        triggered=triggered,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        trigger_time=trigger_time,
    )

@app.get("/abcd", response_model=ABCDResponse)
def abcd(
    ticker: str = Query(..., description="Ticker symbol, e.g. SBER"),
    uid: str    = Query(..., description="Instrument UID in Tinkoff Invest API"),
    date_: str | None = Query(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Trade date in YYYY-MM-DD format (UTC). Defaults to today.",
    ),
):
    """Analyze the ABCD pattern for the given ticker on the specified date (UTC)."""
    # Parse the date or use today if not provided
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

    # Analyze the ABCD pattern on both timeframes
    result_1m = _analyze_abcd(candles_1m)
    result_5m = _analyze_abcd(candles_5m)
    return ABCDResponse(
        ticker=ticker,
        date=trade_date,
        abcd_1min=result_1m,
        abcd_5min=result_5m,
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "abcd:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8006)),
        reload=False,
    )
