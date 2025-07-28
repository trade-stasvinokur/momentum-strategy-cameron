

# Flat‑Breakout – внутридневная стратегия Flat‑Top / Flat‑Bottom

**Flat‑Breakout** — HTTP‑сервис (FastAPI), который *для заданного тикера и дня* определяет, сработали ли паттерны плоского пробоя вверх (Flat‑Top) и пробоя вниз (Flat‑Bottom) на 1‑ и 5‑минутных графиках.

1. Получает исторические свечи (Tinkoff Invest API) 1 мин и 5 мин за торговый день.
2. Выделяет горизонтальные уровни сопротивления/поддержки, к которым цена возвращалась **≥ 2 раз**.
3. Ищет первый пробой/пробой вниз этих уровней.
4. Возвращает JSON с результатами для четырёх комбинаций:
   * Flat‑Top 1 м
   * Flat‑Bottom 1 м
   * Flat‑Top 5 м
   * Flat‑Bottom 5 м  
   Для каждого: `triggered`, `entry_price`, `stop_price`, `trigger_time`.

Сервис упакован в Docker‑контейнер и может работать автономно или вызываться оркестратором.

---

## ⚙️ Быстрый старт

### 1. Подготовка окружения

1. Установите **Python ≥ 3.13**
2. Скопируйте `.env.example` → `.env` и задайте переменные:

```dotenv
# Токен Tinkoff Invest API
TINKOFF_INVEST_TOKEN=your_token_here
# Часовой пояс (по умолчанию Moscow)
TZ=Europe/Moscow
# Порт, который слушает сервис
PORT=8003
```

3. Установите зависимости:

```bash
pip install uv
```

### 2. Запуск локально

```bash
uv run uvicorn flat_breakout:app --host 0.0.0.0 --port 8003
```

Проверь запросом:

```bash
curl -s "http://localhost:8003/flat-breakout?ticker=SBER&uid=f0eac4d5-4753-4c05-857e-676f25c18e68&date=2025-07-28"
```

Пример ответа:

```json
{
  "ticker": "SBER",
  "date": "2025-07-28",
  "flat_top_1min": {
    "triggered": false,
    "entry_price": null,
    "stop_price": null,
    "trigger_time": null
  },
  "flat_bottom_1min": {
    "triggered": false,
    "entry_price": null,
    "stop_price": null,
    "trigger_time": null
  },
  "flat_top_5min": {
    "triggered": false,
    "entry_price": null,
    "stop_price": null,
    "trigger_time": null
  },
  "flat_bottom_5min": {
    "triggered": false,
    "entry_price": null,
    "stop_price": null,
    "trigger_time": null
  }
}
```

### 3. Запуск в Docker

```bash
docker compose up flat_breakout
```

`docker-compose.yml` уже содержит сервис **flat_breakout** с публикацией порта `8003`.

---

## 🧰 Конфигурация

| Переменная              | Обязательно | По умолчанию    | Назначение                              |
| ----------------------- | ----------- | --------------- | --------------------------------------- |
| `TINKOFF_INVEST_TOKEN`  | да          | —               | Доступ к Market Data API Tinkoff        |
| `TZ`                    | нет         | `Europe/Moscow` | Локальная тайзона для логов             |
| `PORT`                  | нет         | `8003`          | Порт, на котором слушает FastAPI        |

---

## 🔄 Как это работает

1. Клиент делает **GET** `/flat-breakout?ticker&uid&date`.
2. Сервис запрашивает свечи:
   * `CANDLE_INTERVAL_1_MIN`
   * `CANDLE_INTERVAL_5_MIN`  
   в диапазоне *[00:00 UTC выбранного дня; 00:00 UTC следующего дня]*.
3. Для каждой серии свечей алгоритм:
   1. Проходит последовательно, запоминая максимум (для Flat‑Top) или минимум (для Flat‑Bottom), который встречался **≥ 2 раз**.
   2. Как только появляется свеча, пробивающая (или пробивающая вниз) этот уровень, формируется:
      * `entry_price` — уровень сопротивления/поддержки;
      * `stop_price` — ближайший противоположный экстремум после последнего касания;
      * `trigger_time` — штамп времени пробойной свечи.
4. Результаты сериализуются через Pydantic и возвращаются клиенту.
5. В логи (`INFO`) пишутся ключевые события; ошибки API — уровнем `ERROR`.

---

## 🗄️ Логи и хранение данных

По умолчанию сервис пишет в stdout → `docker logs flat_breakout`.  
При запуске `uvicorn` можно увеличить подробность логов: `--log-level debug` — будут видны все входящие свечи и пошаговый ход алгоритма.

---

## 📂 Структура проекта

```
flat_breakout/
┣ flat_breakout.py          # FastAPI‑приложение и логика стратегии
┣ DockerfileFlatBreakout    # python:3.13‑slim, ENTRYPOINT ["uv", "run", "uvicorn ..."]
┗ README_FlatBreakout.md    # этот файл
```

---

### ✨ Готово

После `docker compose up` окружение выглядит так:

* **flat_breakout** — REST API стратегии Flat‑Breakout
* **orchestrator** — планировщик, который вызывает сервисы и логирует результат.

Интегрируйте сервис в свои трейдинг‑скрипты или вызывайте напрямую через cURL/Postman.