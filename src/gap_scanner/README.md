# Gap Scanner API – REST‑сервис на FastAPI для поиска утренних «гэпов»

**Gap Scanner API** — это микросервис на FastAPI, который сканирует российский рынок акций через Tinkoff Invest API и возвращает бумаги, открывшиеся с разрывом (gap‑up) не меньше заданного порога.

---

## ⚙️ Быстрый старт

### 1. Подготовка окружения

1. Установите **Python ≥ 3.13**.
2. Скопируйте файл `.env.example` → `.env` и впишите:
   ```dotenv
   TINKOFF_INVEST_TOKEN=ВАШ_ТОКЕН
   ```
3. Установите зависимости:
   ```bash
   pip install fastapi "uvicorn[standard]" tinkoff-invest python-dotenv
   # или через uv
   # pip install uv && uv pip install fastapi "uvicorn[standard]" tinkoff-invest python-dotenv
   ```

### 2. Запуск локально

```bash
uvicorn gap_scanner.gap_scanner:app --reload
```

Сервер откроется на `http://127.0.0.1:8000/`.

* Swagger‑документация: `http://127.0.0.1:8000/docs`
* ReDoc: `http://127.0.0.1:8000/redoc`

### 3. Запуск в Docker

```bash
docker compose up --build
```

Файл `docker-compose.yml` уже пробрасывает порт 8000 наружу, так что API будет доступен по `http://localhost:8000/`.

---

## 🛠️ API Reference

### `GET /gap-up`

| Параметр | Тип | Значение по умолчанию | Описание |
|----------|-----|-----------------------|-----------|
| `min_gap` | float | `0.10` | Минимальный утренний gap (0.10 = 10 %) |
| `date` | `YYYY-MM-DD` | сегодня (UTC) | Дата торгов |

#### Пример запроса

```bash
curl "http://localhost:8000/gap-up?min_gap=0.12&date=2025-07-27"
```

#### Пример ответа

```json
{
  "count": 1,
  "results": [
    {
      "ticker": "CHMK",
      "figi": "BBG000RP8V70",
      "uid": "b5e26096-d013-48e4-b2a9-2f38b6090feb",
      "prev_close": 4880.0,
      "open": 4990.0,
      "gap": 0.0225,
      "gap_test": true
    }
  ]
}
```

* Поле `gap_test` = `true`, если никаких бумаг с реальным гэпом ≥ `min_gap` не найдено и в ответ подставлена акция с максимальным разрывом для back‑test‑анализа.

---

## 📦 Docker‑образ

Собирается из `src/gap_scanner/Dockerfile` (Python slim 3.13). Основные шаги:

1. Копирование исходников в `/app`.
2. Установка зависимостей.
3. Экспорт порта 8000.
4. Запуск `uvicorn gap_scanner.gap_scanner:app`