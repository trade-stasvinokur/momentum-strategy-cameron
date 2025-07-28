import os
import logging
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from tinkoff.invest import Client, CandleInterval, HistoricCandle, Quotation
from dotenv import load_dotenv


def _quote_to_float(q: "Quotation") -> float:
    """Convert a Tinkoff Quotation object to a native Python float."""
    return q.units + q.nano / 1e9

_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)  # load variables from .env into environment
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
    title="Gap-and-Go API",
    description=(
        "Gap-and-Go strategy microservice: identifies whether the Gap-and-Go "
        "pattern triggered for a given ticker on the specified date."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class GapAndGoResponse(BaseModel):
    ticker: str
    date: date
    first_candle_high: float
    first_candle_low: float
    triggered: bool
    entry_price: float | None = None
    stop_price: float | None = None
    trigger_time: datetime | None = None


def _utc_open_close_bounds(trade_date: date) -> tuple[datetime, datetime]:
    """Return UTC start and end timestamps for fetching the first few minutes.

    Moscow Exchange equities session starts at 10:00 MSK (07:00 UTC). We fetch
    the first 10 minutes to ensure we capture the first 5 full candles.
    """
    session_open_utc = datetime.combine(trade_date, time(7, 0, tzinfo=timezone.utc))
    session_end_utc = session_open_utc + timedelta(minutes=10)
    return session_open_utc, session_end_utc


def _fetch_first_minutes(uid: str, start: datetime, end: datetime) -> list[HistoricCandle]:
    """Fetch minute candles between *start* and *end* timestamps."""
    try:
        with Client(TOKEN) as client:
            candles: list[HistoricCandle] = client.market_data.get_candles(
                instrument_id=uid,
                from_=start,
                to=end,
                interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
            ).candles
    except Exception as exc:
        logging.exception("Tinkoff Invest API error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return candles


@app.get("/gap-and-go", response_model=GapAndGoResponse)
def gap_and_go(
    ticker: str = Query(..., description="Ticker symbol, e.g. SBER"),
    uid: str = Query(..., description="Instrument UID in Tinkoff Invest API"),
    date_: str | None = Query(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Trade date in YYYY-MM-DD format (UTC). Defaults to today.",
    ),
):
    """Assess whether the Gap-and-Go pattern triggered for *ticker* on *date_*."""
    try:
        trade_date: date = (
            datetime.strptime(date_, "%Y-%m-%d").date() if date_ else datetime.utcnow().date()
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date format") from exc

    start, end = _utc_open_close_bounds(trade_date)
    candles = _fetch_first_minutes(uid, start, end)
    logging.info("Fetched %d candles for %s on %s", len(candles), ticker, trade_date)

    if not candles:
        raise HTTPException(
            status_code=500,
            detail=f"No minute candles for {ticker} on {trade_date}",
        )

    first_candle = candles[0]
    fh = _quote_to_float(first_candle.high)
    fl = _quote_to_float(first_candle.low)
    first_open = _quote_to_float(first_candle.open)
    first_close = _quote_to_float(first_candle.close)
    # Log OHLC of the first five 1‑minute candles
    for idx, candle in enumerate(candles[:5], start=1):
        logging.info(
            "Candle %d (%s) OHLC: open=%.4f high=%.4f low=%.4f close=%.4f",
            idx,
            candle.time.isoformat(),
            _quote_to_float(candle.open),
            _quote_to_float(candle.high),
            _quote_to_float(candle.low),
            _quote_to_float(candle.close),
        )

    triggered = False
    entry_price: float | None = None
    stop_price: float | None = None
    trigger_ts: datetime | None = None

    # Iterate over the next four candles (minutes 2–5)
    for idx, candle in enumerate(candles[1:5], start=2):
        ch = _quote_to_float(candle.high)
        logging.debug("Candle %d high=%.4f vs first_high=%.4f", idx, ch, fh)
        if ch > fh:
            triggered = True
            entry_price = fh
            stop_price = fl
            trigger_ts = candle.time
            logging.info(
                "Gap-and-Go triggered at candle %d (time=%s): high %.4f crossed first_high %.4f",
                idx,
                candle.time.isoformat(),
                ch,
                fh,
            )
            break

    if not triggered:
        logging.info(
            "Gap-and-Go NOT triggered in first 5 minutes for %s on %s",
            ticker,
            trade_date,
        )
    return GapAndGoResponse(
        ticker=ticker,
        date=trade_date,
        first_candle_high=fh,
        first_candle_low=fl,
        triggered=triggered,
        entry_price=entry_price,
        stop_price=stop_price,
        trigger_time=trigger_ts,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "gap_and_go:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8002)),
        reload=False,
    )
