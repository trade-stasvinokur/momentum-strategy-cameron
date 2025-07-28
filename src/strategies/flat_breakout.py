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
    """Convert a Tinkoff Quotation object to float."""
    return q.units + q.nano / 1e9

# Load environment variables
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

app = FastAPI(
    title="Flat-Top/Flat-Bottom Breakout API",
    description=(
        "Flat-Top/Flat-Bottom Breakout strategy microservice: identifies horizontal breakout patterns "
        "for a given ticker on the specified date (1-minute and 5-minute intervals)."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class PatternResult(BaseModel):
    triggered: bool
    entry_price: float | None = None
    stop_price: float | None = None
    trigger_time: datetime | None = None

class FlatBreakoutResponse(BaseModel):
    ticker: str
    date: date
    flat_top_1min: PatternResult
    flat_bottom_1min: PatternResult
    flat_top_5min: PatternResult
    flat_bottom_5min: PatternResult

def _fetch_candles(uid: str, trade_date: date, interval: CandleInterval) -> list[HistoricCandle]:
    """Fetch historical candles for the given date and interval."""
    # Tinkoff API: from midnight UTC of that date to midnight of next day UTC
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

def _analyze_flat_top(candles: list[HistoricCandle]) -> PatternResult:
    """Analyze flat-top breakout in the given candle series. Returns PatternResult."""
    triggered = False
    entry_price = None
    stop_price = None
    trigger_time = None

    highs = [_quote_to_float(c.high) for c in candles]
    lows = [_quote_to_float(c.low) for c in candles]

    # Iterate through candles to find the first breakout above a repeated resistance level
    max_so_far = highs[0]  # track max high seen so far
    seen_highs = {highs[0]: 1}  # counts of each high value seen

    for i in range(1, len(candles)):
        h = highs[i]
        seen_highs[h] = seen_highs.get(h, 0) + 1  # update count for this high
        if h > max_so_far:
            prev_max = max_so_far
            max_so_far = h
            # Check if the previous max level was touched at least twice (flat resistance)
            if seen_highs.get(prev_max, 0) >= 2:
                # Breakout above prev_max occurred at candle i
                triggered = True
                entry_price = prev_max
                # Find last index where high == prev_max (the last touch before breakout)
                last_touch_index = max(j for j, hj in enumerate(highs[:i]) if abs(hj - prev_max) < 1e-9)
                # Determine stop as nearest low before breakout (after last touch)
                if last_touch_index < i - 1:
                    # find minimum low between last_touch_index+1 and i-1
                    stop_price = min(lows[last_touch_index + 1 : i])
                else:
                    # breakout happened immediately on the next candle
                    stop_price = lows[last_touch_index]
                trigger_time = candles[i].time  # timestamp of breakout candle
                logging.info(
                    f"Flat-Top breakout triggered at {trigger_time.isoformat()} "
                    f"level={entry_price:.4f}, stop={stop_price:.4f}"
                )
                break
    return PatternResult(
        triggered=triggered,
        entry_price=entry_price,
        stop_price=stop_price,
        trigger_time=trigger_time,
    )

def _analyze_flat_bottom(candles: list[HistoricCandle]) -> PatternResult:
    """Analyze flat-bottom breakdown in the given candle series. Returns PatternResult."""
    triggered = False
    entry_price = None
    stop_price = None
    trigger_time = None

    lows = [_quote_to_float(c.low) for c in candles]
    highs = [_quote_to_float(c.high) for c in candles]

    min_so_far = lows[0]
    seen_lows = {lows[0]: 1}

    for i in range(1, len(candles)):
        lo = lows[i]
        seen_lows[lo] = seen_lows.get(lo, 0) + 1
        if lo < min_so_far:
            prev_min = min_so_far
            min_so_far = lo
            # Check if the previous min level was touched at least twice (flat support)
            if seen_lows.get(prev_min, 0) >= 2:
                # Breakdown below prev_min occurred at candle i
                triggered = True
                entry_price = prev_min
                last_touch_index = max(j for j, lj in enumerate(lows[:i]) if abs(lj - prev_min) < 1e-9)
                if last_touch_index < i - 1:
                    stop_price = max(highs[last_touch_index + 1 : i])
                else:
                    stop_price = highs[last_touch_index]
                trigger_time = candles[i].time
                logging.info(
                    f"Flat-Bottom breakdown triggered at {trigger_time.isoformat()} "
                    f"level={entry_price:.4f}, stop={stop_price:.4f}"
                )
                break
    return PatternResult(
        triggered=triggered,
        entry_price=entry_price,
        stop_price=stop_price,
        trigger_time=trigger_time,
    )

@app.get("/flat-breakout", response_model=FlatBreakoutResponse)
def flat_breakout(
    ticker: str = Query(..., description="Ticker symbol, e.g. SBER"),
    uid: str = Query(..., description="Instrument UID in Tinkoff Invest API"),
    date_: str | None = Query(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Trade date in YYYY-MM-DD format (UTC). Defaults to today.",
    ),
):
    """Analyze Flat-Top/Flat-Bottom breakout patterns for the given ticker on the given date."""
    try:
        trade_date = datetime.strptime(date_, "%Y-%m-%d").date() if date_ else datetime.utcnow().date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date format") from exc

    # Fetch 1-minute and 5-minute candles for the day
    candles_1m = _fetch_candles(uid, trade_date, CandleInterval.CANDLE_INTERVAL_1_MIN)
    candles_5m = _fetch_candles(uid, trade_date, CandleInterval.CANDLE_INTERVAL_5_MIN)
    logging.info("Fetched %d candles (1min) and %d candles (5min) for %s on %s",
                 len(candles_1m), len(candles_5m), ticker, trade_date)

    result_top_1m = _analyze_flat_top(candles_1m)
    result_bottom_1m = _analyze_flat_bottom(candles_1m)
    result_top_5m = _analyze_flat_top(candles_5m)
    result_bottom_5m = _analyze_flat_bottom(candles_5m)

    return FlatBreakoutResponse(
        ticker=ticker,
        date=trade_date,
        flat_top_1min=result_top_1m,
        flat_bottom_1min=result_bottom_1m,
        flat_top_5min=result_top_5m,
        flat_bottom_5min=result_bottom_5m,
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "flat_breakout:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8003)),
        reload=False,
    )
