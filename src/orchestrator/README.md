# Orchestrator – ежедневный запуск Gap Scanner → VWAP Levels

**Orchestrator** — фоновый Python‑скрипт, который *каждый торговый день в 10:00 по Москве*:

1. вызывает сервис **Gap Scanner** (`/scan`) и получает список акций с положительным гэпом;
2. берёт акции‑лидеров (max gap) и передаёт её в сервис **VWAP Levels** (`/vwap`);
3. пишет результаты (тикер, gap %, VWAP, S/R) в логи.

Это не HTTP‑API, а **cron‑подобный воркер**: живёт в контейнере, «просыпается» по расписанию (APScheduler) и завершает работу до следующего дня.

---

## ⚙️ Быстрый старт

### 1. Подготовка окружения

1. Установите **Python ≥ 3.13**
2. Скопируйте `.env.example` → `.env` и задайте URL’ы сервисов (по умолчанию Docker DNS‑имена):

```dotenv
# куда стучаться за гэпами
GAP_SCANNER_URL=http://gap_scanner:8000/scan
# куда слать тикер для VWAP
VWAP_LEVELS_URL=http://vwap_levels:8001/vwap
# локальный часовой пояс (рекомендуется оставить Moscow)
TZ=Europe/Moscow
```

3. Установите зависимости:

```bash
pip install uv
```

### 2. Запуск локально

```bash
uv run orchestrator/orchestrator.py
```

Логи появятся в stdout, пример:

```
2025-07-28 10:00:02 INFO Лидер по гэпу: CHMK  gap=3.84%
2025-07-28 10:00:07 INFO CHMK VWAP=4 997.32  S=4 985.11  R=5 009.53
```

### 3. Запуск в Docker

```bash
docker compose up orchestrator
```

`docker-compose.yml` уже содержит контейнер **orchestrator** и ставит его в зависимость от `gap-scanner` и `vwap-levels`.
Планировщик внутри контейнера сам перейдёт в спящий режим до 10:00 МСК.

---

## 🧰 Конфигурация

| Переменная        | Обязательно | По умолчанию                   | Что даёт                  |
| ----------------- | ----------- | ------------------------------ | ------------------------- |
| `GAP_SCANNER_URL` | нет         | `http://gap-scanner:8000/scan` | GET‑эндпоинт Gap Scanner  |
| `VWAP_LEVELS_URL` | нет         | `http://vwap-levels:8001/vwap` | POST‑эндпоинт VWAP Levels |
| `MIN_GAP`         | нет         | `0.10` (10 %)                  | Порог гэпа для выбора     |
| `TZ`              | нет         | `Europe/Moscow`                | Часовой пояс планировщика |

---

## 🔄 Как это работает

1. **APScheduler** (CronTrigger) ждёт 10:00 МСК.
2. Запрос: `GET ${GAP_SCANNER_URL}?min_gap=${MIN_GAP}`.
3. JSON‑ответ сортируется, берётся `results`.
4. Запрос: `GET ${VWAP_LEVELS_URL}` с JSON `{ticker, uid, date}`.
5. Ответ (`vwap`, `support`, `resistance`) выводится в журнал.
6. Скрипт «спит» до следующего триггера.

Падение одного из HTTP‑запросов логируется уровнем `ERROR`, ретраев нет — задача выполняется только один раз за сессию.

---

## 🗄️ Логи и хранение данных

*По‑умолчанию* оркестратор пишет только в stdout → `docker logs orchestrator`.

---

## 📂 Структура проекта

```
orchestrator/
┣ orchestrator.py         # основной скрипт
┣ pyproject.toml          # настройки для пакетного менеджера uv    
┗ Dockerfile              # python:3.13‑slim, ENTRYPOINT ["uv", "run", "orchestrator.py"]
```

---

### ✨ Готово

После поднятия стека `docker compose up` все три контейнера образуют связку:

* **gap‑scanner** — REST API для утренних гэпов
* **vwap‑levels** — REST API для расчёта VWAP
* **orchestrator** — планировщик, клеящий оба сервиса каждый день ровно в 10:00 по Москве.
