import os
import logging
import json
import requests
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
    load_dotenv(dotenv_path=_env_path)
else:
    raise RuntimeError(".env file not found. Please create a .env file")

# ---------------------------------------------------------------------------
# Upstream service URLs (override via docker‑compose environment variables)
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

            # Логируем результаты для 1min и 5min таймфреймов
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
    # Формирование Markdown-отчёта (table style)
    # ------------------------------
    reports_dir = Path(__file__).resolve().parent / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    file_path = reports_dir / f"strategy_results_{date.today():%d-%m-%Y}.md"

    # --- группируем результаты {ticker: {...}} ---
    grouped: dict[str, dict] = {}
    for tick, strat_name, res in results_rows:
        # ① сохраняем prev_close и open из gaps
        grp = grouped.setdefault(
            tick,
            {
                "gap_pct": gap_pct,
                "prev_close": next(g["prev_close"] for g in gaps["results"] if g["ticker"] == tick),
                "open":       next(g["open"]       for g in gaps["results"] if g["ticker"] == tick),
                "strategies": [],
            },
        )
        grp["strategies"].append((strat_name, res))

    md_lines: list[str] = []

    for tick, data in grouped.items():
        # Заголовок тикера
        md_lines.append(f"## 📌 {date.today():%d-%m-%Y} — Ticker: {tick}")
        md_lines.append(
            f"> Gap ↑ {data['gap_pct']:.2f}% "
            f"(prev close {data['prev_close']:.2f} → open {data['open']:.2f})"
        )
        md_lines.append("")

        # -------- таблица по стратегиям --------
        md_lines.append("### 📊 Strategy Results")
        header = "| Strategy | Status | Entry | Stop | Target | P/L (1 shr) | Time |"
        separator = "|----------|:-----:|------:|-----:|-------:|------------:|:----:|"
        rows: list[str] = []
        for strat_name, res in data["strategies"]:
            if strat_name == "VWAP Levels":
                vwap_block = res  # позже покажем отдельно
                continue

            status = "✅" if res.get("triggered") else "❌"
            entry = f"{res.get('entry_price', '—'):.2f}" if res.get("entry_price") else "—"
            stop = f"{res.get('stop_price', '—'):.2f}" if res.get("stop_price") else "—"
            target = (
                f"{res.get('target_price'):.2f}" if res.get("target_price") else "—"
            )

            # P/L
            if res.get("entry_price") and res.get("stop_price"):
                loss = res["entry_price"] - res["stop_price"]
                profit = (
                    res["target_price"] - res["entry_price"]
                    if res.get("target_price")
                    else loss
                )
                pl = f"+{profit:.2f}/-{loss:.2f}"
            else:
                pl = "—"

            # Time UTC→MSK
            if res.get("trigger_time"):
                try:
                    dt_utc = datetime.fromisoformat(res["trigger_time"])
                    dt_msk = dt_utc.astimezone(ZoneInfo("Europe/Moscow"))
                    time_str = dt_msk.strftime("%H:%M")
                except Exception:
                    time_str = "—"
            else:
                time_str = "—"

            rows.append(
                f"| {strat_name} | {status} | {entry} | {stop} | "
                f"{target} | {pl} | {time_str} |"
            )

        # собираем таблицу
        md_lines.extend([header, separator, *rows, ""])

        # --- VWAP-уровни с указанием σ ---
        if vwap_block:
            vwap   = vwap_block.get("vwap")
            sup    = vwap_block.get("support")
            res    = vwap_block.get("resistance")

            # Базовая σ — расстояние до ближайшей полосы
            if all((vwap, sup, res)):
                sigma = min(abs(vwap - sup), abs(res - vwap))
                # избегаем деления на 0
                sigma = sigma if sigma else None
            else:
                sigma = None

            # Функция-помощник для подписи уровня
            def fmt(level: float, sign: str) -> str:
                if sigma:
                    n = round(abs(level - vwap) / sigma)
                    return f"{level:.2f} ({sign}{n}σ)"
                return f"{level:.2f}"

            md_lines.extend(
                [
                    "### 📊 VWAP & Key Levels",
                    f"VWAP {vwap:.2f}",
                    f"Support {fmt(sup, '−')}",
                    f"Resistance {fmt(res, '+')}",
                    "",
                ]
            )

        # горизонтальный разделитель между тикерами
        md_lines.append("---\n")

    # запись файла
    file_path.write_text("\n".join(md_lines), encoding="utf-8")
    logging.info("Markdown-отчёт сохранён: %s", file_path)


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
