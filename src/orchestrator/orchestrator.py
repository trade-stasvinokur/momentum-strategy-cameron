import os, logging, requests
from pathlib import Path
from datetime import datetime, date
from zoneinfo import ZoneInfo
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load environment variables from .env
# ---------------------------------------------------------------------------
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)  # load variables from .env into environment
else:
    raise RuntimeError(".env file not found. Please create a .env file")



# URLs сервисов берём из переменных окружения (см. docker‑compose)
SCAN_URL = os.getenv("GAP_SCANNER_URL", "http://gap-scanner:8000/gap-up")
VWAP_URL = os.getenv("VWAP_LEVELS_URL", "http://vwap-levels:8001/vwap")
GAP_AND_GO_URL = os.getenv("GAP_AND_GO_URL", "http://gap-and-go:8002/gap-and-go")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler()]
)

def run():
    today = date.today().isoformat()                # YYYY‑MM‑DD
    params = {"min_gap": 0.10, "date": today}       # если в API есть date
    try:
        resp = requests.get(SCAN_URL, params=params, timeout=600)
        resp.raise_for_status()
        gaps = resp.json()
    except Exception as e:
        logging.error(f"Gap‑scanner: {e}")
        return

    if not gaps:
        logging.info("Нет акций с гэпом выше порога.")
        return

    for gap in gaps.get('results'):
        ticker, uid, gap_pct = gap["ticker"], gap["uid"], gap["gap"] * 100
        logging.info(f"Лидер по гэпу: {ticker}  gap={gap_pct:.2f}%")

        params = {"ticker": ticker, "uid": uid, "date": today}
        try:
            r = requests.get(VWAP_URL, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            logging.info(
                f"{ticker} VWAP={data['vwap']:.2f}  "
                f"S={data['support']:.2f}  R={data['resistance']:.2f}"
            )
        except Exception as e:
            logging.error(f"VWAP‑levels: {e}")

        # ------------------------------
        # Gap‑and‑Go strategy analysis
        # ------------------------------
        try:
            gag_resp = requests.get(GAP_AND_GO_URL, params=params, timeout=60)
            gag_resp.raise_for_status()
            gag = gag_resp.json()

            if gag.get("triggered"):
                logging.info(
                    "%s Gap&Go TRIGGERED ‑ entry %.2f stop %.2f at %s",
                    ticker,
                    gag["entry_price"],
                    gag["stop_price"],
                    gag["trigger_time"],
                )
            else:
                logging.info(
                    "%s Gap&Go not triggered. First candle H/L: %.2f / %.2f",
                    ticker,
                    gag["first_candle_high"],
                    gag["first_candle_low"],
                )

        except Exception as e:
            logging.error(f"Gap‑and‑Go: {e}")

if __name__ == "__main__":
    run()
    sched = BlockingScheduler()
    # 10:00 Европы/Москва (UTC+3 летом, UTC+3 зимой с 2014 г.)
    sched.add_job(run, CronTrigger(hour=10, minute=0, timezone="Europe/Moscow"))
    logging.info("Orchestrator started, waiting for 10:00 MSK …")
    sched.start()
