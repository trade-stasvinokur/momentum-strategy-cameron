import os
import csv
import logging
import requests
from pathlib import Path
from datetime import datetime, date
from zoneinfo import ZoneInfo
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
import tempfile
import shutil

# ---------------------------------------------------------------------------
# Load environment variables from .env
# ---------------------------------------------------------------------------
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)
else:
    raise RuntimeError(".env file not found. Please create a .env file")

# ---------------------------------------------------------------------------
# Upstream service URLs (override via docker-compose environment variables)
# ---------------------------------------------------------------------------
SCAN_URL = os.getenv("GAP_SCANNER_URL", "http://gap_scanner:8000/gap-up")
VWAP_URL = os.getenv("VWAP_LEVELS_URL", "http://vwap_levels:8001/vwap")
GAP_AND_GO_URL = os.getenv("GAP_AND_GO_URL", "http://gap_and_go:8002/gap-and-go")
FLAT_BREAKOUT_URL = os.getenv("FLAT_BREAKOUT_URL", "http://flat_breakout:8003/flat-breakout")
BULL_FLAG_URL = os.getenv("BULL_FLAG_URL", "http://bull_flag:8004/bull-flag")
FIRST_PULLBACK_URL = os.getenv("FIRST_PULLBACK_URL", "http://first_pullback:8005/first-pullback")
ABCD_URL = os.getenv("ABCD_URL", "http://abcd:8006/abcd")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler()],
)

# ---------------------------------------------------------------------------
# Main orchestration logic
# ---------------------------------------------------------------------------
def run() -> None:
    """Fetch gap list and run all strategies for each gapping ticker."""

    today: str = date.today().isoformat()  # YYYY-MM-DD
    params_gap = {"min_gap": 0.10, "date": today}

    # ------------------------------
    # Gap-scanner
    # ------------------------------
    try:
        resp = requests.get(SCAN_URL, params=params_gap, timeout=1200)
        resp.raise_for_status()
        gaps = resp.json()
    except Exception as exc:  # broad except so orchestrator continues even if scanner fails
        logging.error("Gap-scanner: %s", exc)
        return

    if not gaps or not gaps.get("results"):
        logging.info("Нет акций с гэпом выше порога.")
        return

    results_rows: list = []  # collect results for report

    # -------------------------------------------------------------------
    # Iterate over each gapping ticker and query all analytical services
    # -------------------------------------------------------------------
    for gap in gaps["results"]:
        ticker: str = gap["ticker"]
        uid: str = gap["uid"]
        gap_pct: float = gap["gap"] * 100
        logging.info("Лидер по гэпу: %s  gap=%.2f%%", ticker, gap_pct)

        params_common = {"ticker": ticker, "uid": uid, "date": today}

        # ------------------------------
        # VWAP Levels
        # ------------------------------
        try:
            r = requests.get(VWAP_URL, params=params_common, timeout=60)
            r.raise_for_status()
            data = r.json()
            logging.info(
                "%s VWAP=%.2f  S=%.2f  R=%.2f",
                ticker,
                data.get("vwap", float("nan")),
                data.get("support", float("nan")),
                data.get("resistance", float("nan")),
            )
            results_rows.append((ticker, "VWAP Levels", data))
        except Exception as exc:
            logging.error("VWAP-levels: %s", exc)

        # ------------------------------
        # Gap-and-Go strategy analysis
        # ------------------------------
        try:
            gag_resp = requests.get(GAP_AND_GO_URL, params=params_common, timeout=60)
            gag_resp.raise_for_status()
            gag = gag_resp.json()
            # Remove redundant fields for report
            gag.pop("ticker", None)
            gag.pop("date", None)

            if gag.get("triggered"):
                logging.info(
                    "%s Gap&Go TRIGGERED – entry %.2f stop %.2f at %s",
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
            results_rows.append((ticker, "Gap&Go", gag))
        except Exception as exc:
            logging.error("Gap-and-Go: %s", exc)

        # ------------------------------
        # Flat-Top / Flat-Bottom Breakout
        # ------------------------------
        try:
            fb_resp = requests.get(FLAT_BREAKOUT_URL, params=params_common, timeout=60)
            fb_resp.raise_for_status()
            fb = fb_resp.json()

            # Iterate over all four patterns in the response
            for label, res_key in [
                ("1m Flat-Top", "flat_top_1min"),
                ("1m Flat-Bottom", "flat_bottom_1min"),
                ("5m Flat-Top", "flat_top_5min"),
                ("5m Flat-Bottom", "flat_bottom_5min"),
            ]:
                res = fb.get(res_key, {})
                if not isinstance(res, dict):
                    continue
                if res.get("triggered"):
                    logging.info(
                        "%s %s TRIGGERED – entry %.2f stop %.2f at %s",
                        ticker,
                        label,
                        res.get("entry_price", float("nan")),
                        res.get("stop_price", float("nan")),
                        res.get("trigger_time"),
                    )
                else:
                    logging.info("%s %s not triggered", ticker, label)
                results_rows.append((ticker, label, res))
        except Exception as exc:
            logging.error("Flat-Breakout: %s", exc)

        # ------------------------------
        # Bull Flag pattern analysis
        # ------------------------------
        try:
            bf_resp = requests.get(BULL_FLAG_URL, params=params_common, timeout=60)
            bf_resp.raise_for_status()
            bf = bf_resp.json()
            # Log results for 1m and 5m timeframes
            for label, res_key in [("1m BullFlag", "bull_flag_1min"),
                                   ("5m BullFlag", "bull_flag_5min")]:
                res = bf.get(res_key, {})
                if not isinstance(res, dict):
                    continue
                if res.get("triggered"):
                    logging.info(
                        "%s %s TRIGGERED – entry %.2f stop %.2f target %.2f at %s",
                        ticker,
                        label,
                        res.get("entry_price", float("nan")),
                        res.get("stop_price", float("nan")),
                        res.get("target_price", float("nan")),
                        res.get("trigger_time")
                    )
                else:
                    logging.info("%s %s not triggered", ticker, label)
                results_rows.append((ticker, label, res))
        except Exception as exc:
            logging.error("BullFlag: %s", exc)

        # ------------------------------
        # First Pullback pattern analysis
        # ------------------------------
        try:
            fp_resp = requests.get(FIRST_PULLBACK_URL, params=params_common, timeout=60)
            fp_resp.raise_for_status()
            fp = fp_resp.json()

            # Log results for 1min and 5min timeframes
            for label, res_key in [("1m FirstPullback", "first_pullback_1min"),
                                   ("5m FirstPullback", "first_pullback_5min")]:
                res = fp.get(res_key, {})
                if not isinstance(res, dict):
                    continue
                if res.get("triggered"):
                    logging.info(
                        "%s %s TRIGGERED – entry %.2f stop %.2f target %.2f at %s",
                        ticker,
                        label,
                        res.get("entry_price", float("nan")),
                        res.get("stop_price", float("nan")),
                        res.get("target_price", float("nan")),
                        res.get("trigger_time")
                    )
                else:
                    logging.info("%s %s not triggered", ticker, label)
                results_rows.append((ticker, label, res))
        except Exception as exc:
            logging.error("FirstPullback: %s", exc)

        # ------------------------------
        # ABCD pattern analysis
        # ------------------------------
        try:
            abcd_resp = requests.get(ABCD_URL, params=params_common, timeout=60)
            abcd_resp.raise_for_status()
            abcd = abcd_resp.json()
            for label, res_key in [("1m ABCD", "abcd_1min"), ("5m ABCD", "abcd_5min")]:
                res = abcd.get(res_key, {})
                if not isinstance(res, dict):
                    continue
                if res.get("triggered"):
                    logging.info(
                        "%s %s TRIGGERED – entry %.2f stop %.2f target %.2f at %s",
                        ticker, label,
                        res.get("entry_price", float("nan")),
                        res.get("stop_price", float("nan")),
                        res.get("target_price", float("nan")),
                        res.get("trigger_time")
                    )
                else:
                    logging.info("%s %s not triggered", ticker, label)
                results_rows.append((ticker, label, res))
        except Exception as exc:
            logging.error("ABCD: %s", exc)

    # ------------------------------
    # Формирование CSV-отчёта
    # ------------------------------
    reports_dir = Path(__file__).resolve().parent / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    file_path = reports_dir / "strategy_results.csv"

    # ---- Проверяем, нужно ли писать сегодняшние строки ----
    today = date.today().isoformat()
    if file_path.exists():
        with file_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            existing_header = next(reader, [])
            first_data_row = next(reader, [])
        if first_data_row and first_data_row[0] == today:
            logging.info("Данные за %s уже есть – файл не изменён.", today)
            return

    # --- группируем результаты {ticker: {...}} ---
    grouped: dict[str, dict] = {}
    for tick, strat_name, res in results_rows:
        gap_data = next((g for g in gaps["results"] if g["ticker"] == tick), {})
        grp = grouped.setdefault(
            tick,
            {
                "gap": gap_data.get("gap"),
                "prev_close": gap_data.get("prev_close"),
                "open": gap_data.get("open"),
                "strategies": [],
            },
        )
        grp["strategies"].append((strat_name, res))

    csv_rows: list[list] = []
    header = [
        "date",
        "ticker",
        "gap",
        "prev_close",
        "open",
        "strategy",
        "status",
        "entry",
        "stop",
        "target",
        "pl",
        "time",
        "vwap",
        "support",
        "resistance",
    ]

    for tick, data in grouped.items():
        # Ищем блок VWAP Levels (может быть None, если сервис не ответил)
        vwap_block = next(
            (res for (strat_name, res) in data["strategies"] if strat_name == "VWAP Levels"),
            {},
        )
        vwap_val = vwap_block.get("vwap", "")
        sup_val = vwap_block.get("support", "")
        res_val = vwap_block.get("resistance", "")

        gap_raw = data.get("gap")
        gap_pct = f"{gap_raw * 100:.2f}" if isinstance(gap_raw, (int, float)) else ""
        prev_cls = f"{data['prev_close']:.2f}" if isinstance(data.get("prev_close"), (int, float)) else ""
        open_val = f"{data['open']:.2f}" if isinstance(data.get("open"), (int, float)) else ""

        # Формируем строки по всем стратегиям, исключая сам блок VWAP Levels
        for strat_name, res in data["strategies"]:
            if strat_name == "VWAP Levels":
                continue

            status_emoji = "✅" if res.get("triggered") else "❌"
            entry = f"{res.get('entry_price', ''):.2f}" if res.get("entry_price") else ""
            stop = f"{res.get('stop_price', ''):.2f}" if res.get("stop_price") else ""
            target = f"{res.get('target_price', ''):.2f}" if res.get("target_price") else ""

            # P/L (только потенциальная прибыль, как в примере)
            if res.get("triggered") and res.get("entry_price") and res.get("target_price"):
                pl_val = f"+{res['target_price'] - res['entry_price']:.2f}"
            else:
                pl_val = ""

            # Время (UTC → Moscow) в HH:MM
            if res.get("trigger_time"):
                try:
                    dt_utc = datetime.fromisoformat(res["trigger_time"])
                    dt_msk = dt_utc.astimezone(ZoneInfo("Europe/Moscow"))
                    time_val = dt_msk.strftime("%H:%M")
                except Exception:
                    time_val = ""
            else:
                time_val = ""

            csv_rows.append(
                [
                    today,
                    tick,
                    gap_pct,
                    prev_cls,
                    open_val,
                    strat_name,
                    status_emoji,
                    entry,
                    stop,
                    target,
                    pl_val,
                    time_val,
                    f"{vwap_val:.2f}" if isinstance(vwap_val, (int, float)) else vwap_val,
                    f"{sup_val:.2f}" if isinstance(sup_val, (int, float)) else sup_val,
                    f"{res_val:.2f}" if isinstance(res_val, (int, float)) else res_val,
                ]
            )

    # ---- Собираем старые данные ----
    old_rows = []
    if file_path.exists():
        with file_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            old_header = next(reader, [])
            old_rows = list(reader)

    # ---- Пишем во временный файл, потом атомарно заменяем ----
    with tempfile.NamedTemporaryFile("w",
                                    newline="",
                                    encoding="utf-8",
                                    delete=False) as tmp:
        writer = csv.writer(tmp)
        writer.writerow(header)           # шапка (можно взять existing_header, если она у вас менялась)
        writer.writerows(csv_rows)        # свежие строки
        writer.writerows(old_rows)        # предыдущие записи
        tmp_name = tmp.name

    shutil.move(tmp_name, file_path)
    logging.info("CSV-отчёт обновлён: %s", file_path)


# ---------------------------------------------------------------------------
# Scheduler entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Immediate first run
    run()

    # Then schedule daily at 10:00 MSK (UTC+3)
    sched = BlockingScheduler(timezone=ZoneInfo("Europe/Moscow"))
    sched.add_job(run, CronTrigger(hour=10, minute=0, timezone="Europe/Moscow"))

    logging.info("Orchestrator started – waiting for 10:00 MSK …")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logging.info("Orchestrator stopped.")
