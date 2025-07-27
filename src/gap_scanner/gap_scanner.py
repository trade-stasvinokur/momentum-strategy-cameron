"""gap_scanner.py
~~~~~~~~~~~~~~~~~
Simple module to scan Russian stocks that opened with a gap up of **≥ 10 %**
compared to the previous day close using the Tinkoff Invest API.

Requirements
------------
- invest-python (`pip install tinkoff-invest-api`)
- Environment variable **TINKOFF_INVEST_TOKEN** with your sandbox or live token.
- python-dotenv (`pip install python-dotenv`)

Usage example
-------------
```bash
python -m gap_scanner               # prints gappers for today (UTC)
python -m gap_scanner --date 2025-07-18  --gap 0.12
```

You can also import the helper function inside your own scripts:
```python
from gap_scanner import scan_gap_up
stocks = scan_gap_up(min_gap=0.1)
```
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
from typing import List, Dict

from tinkoff.invest import Client, CandleInterval, InstrumentIdType
from tinkoff.invest.schemas import AssetsRequest
from tinkoff.invest.utils import now

from pathlib import Path
from dotenv import load_dotenv


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
# Helper functions
# ---------------------------------------------------------------------------

def _prev_trading_day(day: _dt.date) -> _dt.date:
    """Return the previous weekday (very rough market calendar)."""
    prev = day - _dt.timedelta(days=1)
    # while prev.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
    #     prev -= _dt.timedelta(days=1)
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
                prev_candles = client.market_data.get_candles(
                    instrument_id=uid,
                    from_=prev_from,
                    to=prev_to,
                    interval=CandleInterval.CANDLE_INTERVAL_DAY,
                ).candles
                if not prev_candles:
                    continue
                prev_close = prev_candles[-1].close
                prev_close_price = prev_close.units + prev_close.nano / 1e9

                # --- today open candle (daily) ---
                today_from = _to_ts(date)
                today_to = _to_ts(date + _dt.timedelta(days=1))
                today_candles = client.market_data.get_candles(
                    instrument_id=uid,
                    from_=today_from,
                    to=today_to,
                    interval=CandleInterval.CANDLE_INTERVAL_DAY,
                ).candles
                if not today_candles:
                    continue
                today_open = today_candles[0].open  # first candle of the day
                today_open_price = today_open.units + today_open.nano / 1e9

                gap = (today_open_price - prev_close_price) / prev_close_price
                print(f'{i}/{total}"ticker": {ticker}'
                      f' "figi": {figi}'
                      f' "uid": {uid}'
                      f' "prev_close": {prev_close_price}'
                      f' "open": {today_open_price}'
                      f' "gap": {gap}')
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

    # Sort by gap desc
    return sorted(result, key=lambda x: x["gap"], reverse=True)


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------

def _main() -> None:
    parser = argparse.ArgumentParser(description="Scan for gap‑up shares (≥10%) using Tinkoff Invest API")
    parser.add_argument("--date", type=lambda s: _dt.datetime.strptime(s, "%Y-%m-%d").date(), help="Date in YYYY-MM-DD; default today (UTC)")
    parser.add_argument("--gap", type=float, default=0.10, help="Minimal gap fraction (0.10 == 10 %)")
    args = parser.parse_args()

    stocks = scan_gap_up(min_gap=args.gap, date=args.date)
    if not stocks:
        print("No gappers found.")
        return

    print(f"Found {len(stocks)} gap‑up shares (≥{args.gap*100:.2f}%):\n")
    for s in stocks:
        print(
            f"{s['ticker']:<10} | Prev close: {s['prev_close']:.2f} | Open: {s['open']:.2f} | Gap: {s['gap']*100:.2f}%"
        )


if __name__ == "__main__":
    _main()
