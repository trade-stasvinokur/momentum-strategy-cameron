import os
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from tinkoff.invest import Client, CandleInterval, HistoricCandle, Quotation
from dotenv import load_dotenv

def _quote_to_float(q: Quotation) -> float:
    """Конвертировать объект Quotation (цены Tinkoff API) в float."""
    return q.units + q.nano / 1e9

# Загрузка переменных окружения (.env) – требуется TINKOFF_INVEST_TOKEN
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)
else:
    raise RuntimeError(".env file not found. Please create a .env file with TINKOFF_INVEST_TOKEN=<your token>")

TOKEN = os.getenv("TINKOFF_INVEST_TOKEN")
if not TOKEN:
    raise RuntimeError("TINKOFF_INVEST_TOKEN is not set in environment")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# Инициализация FastAPI приложения
app = FastAPI(
    title="First Pullback Strategy API",
    description=(
        "Микросервис стратегии First Pullback: ищет паттерн 'первый откат' для указанного тикера на заданную дату. "
        "Анализирует первые минуты торгов на интервалах 1min и 5min (как в моментум-трейдинге Россa Камеронa)."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Модели ответа
class PullbackResult(BaseModel):
    triggered: bool
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    trigger_time: datetime | None = None

class FirstPullbackResponse(BaseModel):
    ticker: str
    date: date
    first_pullback_1min: PullbackResult
    first_pullback_5min: PullbackResult

def _fetch_candles(uid: str, start: datetime, end: datetime, interval: CandleInterval) -> list[HistoricCandle]:
    """Запросить исторические свечи через Tinkoff Invest API для заданного интервала времени."""
    try:
        with Client(TOKEN) as client:
            candles = client.market_data.get_candles(
                instrument_id=uid, from_=start, to=end, interval=interval
            ).candles
    except Exception as exc:
        logging.exception("Tinkoff Invest API error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return candles

def _analyze_first_pullback(candles: list[HistoricCandle]) -> PullbackResult:
    """Анализирует список свечей на наличие паттерна 'First Pullback'. Возвращает PullbackResult."""
    # Если данных недостаточно, паттерн не сработал
    if not candles or len(candles) < 2:
        return PullbackResult(triggered=False)
    # Начальные значения
    open_price = _quote_to_float(candles[0].open)        # цена открытия первой минуты
    peak_price = _quote_to_float(candles[0].high)        # максимум начального импульса
    peak_index = 0
    peak_volume = candles[0].volume

    # 1. Найти конец начального импульса (первая свеча, не обновившая максимум предыдущей)
    pullback_start_idx = None
    for i in range(1, len(candles)):
        curr_high = _quote_to_float(candles[i].high)
        if curr_high > peak_price:
            # продолжается рост импульса
            peak_price = curr_high
            peak_index = i
            if candles[i].volume > peak_volume:
                peak_volume = candles[i].volume  # находим максимальный объём в импульсе
        else:
            # свеча не обновила максимум – начало отката
            pullback_start_idx = i
            break
    if pullback_start_idx is None:
        # За отведённое время цена не откатилась (постоянно росла) – паттерн не сформировался
        return PullbackResult(triggered=False)

    # 2. Анализ отката: найдём минимальную цену отката и определим момент разворота
    pullback_low = _quote_to_float(candles[pullback_start_idx].low)
    last_pullback_high = _quote_to_float(candles[pullback_start_idx].high)
    trigger_idx = None
    for j in range(pullback_start_idx, len(candles) - 1):
        # Обновляем минимум отката
        curr_low = _quote_to_float(candles[j].low)
        if curr_low < pullback_low:
            pullback_low = curr_low
        # Проверяем, не произошёл ли пробой максимума предыдущей свечи (разворот вверх)
        next_high = _quote_to_float(candles[j + 1].high)
        if next_high > _quote_to_float(candles[j].high):
            # Найден сигнал разворота на свечe j+1
            trigger_idx = j + 1
            last_pullback_high = _quote_to_float(candles[j].high)  # уровень входа = хай последней откатной свечи
            break

    # 3. Формируем результат на основе найденных данных
    result = PullbackResult(triggered=False)
    if trigger_idx is not None:
        # Проверка условий качества паттерна:
        # a) Достаточная величина начального импульса (не менее ~3%)
        if peak_price / open_price - 1 < 0.03:
            logging.info(f"Initial move {(peak_price/open_price - 1):.2%} is too small – pattern invalid")
        # b) Откат на пониженном объёме (объём каждой откатной свечи < объёма импульса)
        elif max(c.volume for c in candles[pullback_start_idx:trigger_idx]) >= peak_volume:
            logging.info("Pullback volume is too high (no volume decline during pullback) – pattern invalid")
        # c) Не слишком глубокий откат (цена отката не ушла ниже ~50% роста)
        elif pullback_low < open_price + 0.5 * (peak_price - open_price):
            logging.info("Pullback retraced more than 50% of the initial move – pattern invalid")
        else:
            # Все условия выполнены – паттерн сработал
            result.triggered = True
            result.entry_price = last_pullback_high
            result.stop_price = pullback_low
            result.target_price = peak_price
            result.trigger_time = candles[trigger_idx].time
            logging.info(
                f"First Pullback TRIGGERED at {result.trigger_time.isoformat()} – "
                f"entry={result.entry_price:.2f}, stop={result.stop_price:.2f}, target={result.target_price:.2f}"
            )
    return result

@app.get("/first-pullback", response_model=FirstPullbackResponse)
def first_pullback(
    ticker: str = Query(..., description="Ticker symbol, e.g. SBER"),
    uid: str    = Query(..., description="Instrument UID in Tinkoff Invest API"),
    date_: str | None = Query(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Trade date in YYYY-MM-DD format (UTC). Defaults to today."
    ),
):
    """Проверить наличие паттерна First Pullback для заданного тикера на дату."""
    try:
        trade_date = datetime.strptime(date_, "%Y-%m-%d").date() if date_ else datetime.utcnow().date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date format") from exc

    # Получаем минутные свечи первых 15 минут и 5-минутные свечи первых 30 минут торгового дня
    session_open = datetime.combine(trade_date, time(7, 0, tzinfo=timezone.utc))
    candles_1m = _fetch_candles(uid, session_open, session_open + timedelta(minutes=15), CandleInterval.CANDLE_INTERVAL_1_MIN)
    candles_5m = _fetch_candles(uid, session_open, session_open + timedelta(minutes=30), CandleInterval.CANDLE_INTERVAL_5_MIN)
    logging.info(f"Fetched {len(candles_1m)} candles (1m) and {len(candles_5m)} candles (5m) for {ticker} on {trade_date}")

    # Анализируем паттерн на 1-минутном и 5-минутном интервалах
    result_1m = _analyze_first_pullback(candles_1m)
    result_5m = _analyze_first_pullback(candles_5m)
    return FirstPullbackResponse(
        ticker=ticker,
        date=trade_date,
        first_pullback_1min=result_1m,
        first_pullback_5min=result_5m
    )

# Для локального запуска: uvicorn
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "first_pullback:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8005)),
        reload=False
    )
