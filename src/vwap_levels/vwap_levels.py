from __future__ import annotations
import os
import datetime as _dt
from pathlib import Path
from typing import List, Dict

import pandas as pd
from tinkoff.invest import Client, CandleInterval
from tinkoff.invest.utils import now
from fastapi import FastAPI, HTTPException
import uvicorn

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load environment variables from .env
# ---------------------------------------------------------------------------
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)  # load variables from .env into environment
else:
    raise RuntimeError(".env file not found. Please create a .env file with TINKOFF_INVEST_TOKEN=<your token>")


# ──────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────
# API helpers
# ──────────────────────────────────────────────────────────────
def _to_ts(date_: _dt.date) -> _dt.datetime:
    """UTC‐полночь для указанной даты."""
    return _dt.datetime.combine(date_, _dt.time.min, tzinfo=_dt.timezone.utc)

def _load_minute_candles(uid: str, date_: _dt.date, client: Client) -> pd.DataFrame:
    """Минутные свечи за день для одного инструмента."""
    from_ts = _to_ts(date_)
    to_ts = from_ts + _dt.timedelta(days=1)

    candles = client.market_data.get_candles(
        instrument_id=uid,
        from_=from_ts,
        to=to_ts,
        interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
    ).candles
    if not candles:
        raise ValueError(f"Нет минутных свечей за {date_}")

    df = pd.DataFrame(
        {
            "ts": [c.time for c in candles],
            "close": [
                c.close.units + c.close.nano / 1e9 for c in candles
            ],
            "volume": [c.volume for c in candles],
        }
    )
    return df

def _compute_vwap(df: pd.DataFrame) -> pd.Series:
    pv = (df["close"] * df["volume"]).cumsum()
    vol = df["volume"].cumsum()
    return pv / vol  # Series той же длины

# ──────────────────────────────────────────────────────────────
# Core
# ──────────────────────────────────────────────────────────────
def calc_vwap_levels(
    ticker: str,
    uid: str,
    date_: _dt.date | None = None,
) -> Dict[str, float]:
    """
    Возвращает словарь {'vwap', 'support', 'resistance', 'std'} для тикера
    и сразу пишет строку в БД.
    """
    if date_ is None:
        date_ = now().date()

    token = os.getenv("TINKOFF_INVEST_TOKEN")
    if not token:
        raise RuntimeError("TINKOFF_INVEST_TOKEN не найден в окружении")

    with Client(token) as client:
        df = _load_minute_candles(uid, date_, client)

    df["vwap"] = _compute_vwap(df)
    diff = df["close"] - df["vwap"]
    std = diff.std()

    vwap_today = df["vwap"].iloc[-1]
    support = vwap_today - std
    resistance = vwap_today + std

    return {
        "vwap": vwap_today,
        "support": support,
        "resistance": resistance,
        "std": std,
    }

app = FastAPI(title="VWAP Levels API")

@app.get("/vwap-levels")
def vwap_levels_endpoint(
    ticker: str,
    uid: str,
    date: str | None = None,
):
    """
    Calculate VWAP, support, resistance and std for a given ticker.

    **Parameters**
    - **ticker**: Human‑readable ticker symbol
    - **uid**: Tinkoff instrument UID
    - **date**: Optional trading date in YYYY‑MM‑DD (UTC). Defaults to today.
    """
    try:
        date_parsed = (
            _dt.datetime.strptime(date, "%Y-%m-%d").date() if date else None
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid date format; expected YYYY‑MM‑DD",
        ) from exc

    try:
        return calc_vwap_levels(ticker, uid, date_parsed)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

if __name__ == "__main__":
    uvicorn.run("vwap_levels.vwap_levels:app", host="0.0.0.0", port=8001, reload=True)