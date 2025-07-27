from __future__ import annotations
import os
import datetime as _dt
import sqlite3
from pathlib import Path
from typing import List, Dict

import pandas as pd
from tinkoff.invest import Client, CandleInterval
from tinkoff.invest.utils import now

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load environment variables from .env
# ---------------------------------------------------------------------------
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)  # load variables from .env into environment
else:
    raise RuntimeError(".env file not found. Please create a .env file with TINKOFF_INVEST_TOKEN=<your token>")


# путь к той же БД
DB_PATH = Path(os.getenv("GAP_SCANNER_DB", "")).expanduser().resolve()

# ──────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────
def _init_db() -> None:
    """Создаёт таблицу vwap_levels, если её ещё нет."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vwap_levels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                vwap REAL,
                support REAL,
                resistance REAL,
                std REAL
            )
            """
        )

def _save_level(
    date_: _dt.date,
    ticker: str,
    vwap: float,
    support: float,
    resistance: float,
    std: float,
) -> None:
    _init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO vwap_levels
              (timestamp, date, ticker, vwap, support, resistance, std)
            VALUES
              (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _dt.datetime.utcnow().isoformat(timespec="seconds"),
                date_.isoformat(),
                ticker,
                vwap,
                support,
                resistance,
                std,
            ),
        )
        conn.commit()

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

    # сохранить
    _save_level(date_, ticker, vwap_today, support, resistance, std)

    return {
        "vwap": vwap_today,
        "support": support,
        "resistance": resistance,
        "std": std,
    }

# ──────────────────────────────────────────────────────────────
# Utility: достаём список тикеров-UID из gap_records
# ──────────────────────────────────────────────────────────────
def _tickers_from_gap_db(date_: _dt.date) -> List[Dict[str, str]]:
    sql = """
        SELECT DISTINCT ticker, uid
        FROM gap_records
        WHERE date = :day
    """
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(sql, {"day": date_.isoformat()}).fetchall()
    return [{"ticker": t, "uid": u} for t, u in rows]

# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse, logging

    parser = argparse.ArgumentParser(description="Расчёт VWAP-уровней S/R")
    parser.add_argument(
        "--date",
        type=lambda s: _dt.datetime.strptime(s, "%Y-%m-%d").date(),
        help="Дата YYYY-MM-DD; по умолчанию сегодня (UTC)",
    )
    parser.add_argument(
        "--ticker",
        help="Один тикер; если не задано, берём все тикеры из gap_records",
    )
    args = parser.parse_args()

    target_date = args.date or now().date()
    tickers = (
        [{"ticker": args.ticker, "uid": None}]
        if args.ticker
        else _tickers_from_gap_db(target_date)
    )

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    for item in tickers:
        try:
            res = calc_vwap_levels(item["ticker"], item["uid"], target_date)
            logging.info(
                f"{item['ticker']}: VWAP={res['vwap']:.2f}  "
                f"S={res['support']:.2f}  R={res['resistance']:.2f}"
            )
        except Exception as e:
            logging.error(f"{item['ticker']}: {e}")