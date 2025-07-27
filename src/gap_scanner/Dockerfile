# -------------------------------
# Dockerfile для модуля Gap Scanner
# -------------------------------

# 1) Базовый образ на Python 3.13
FROM python:3.13-slim

# 2) Рабочая директория
WORKDIR /app

# 3) Копируем файлы
COPY . /app/

# 4) Устанавливаем uv и синхронизируем зависимости в системную среду контейнера
RUN pip install --no-cache-dir uv \
    && uv sync

# 5) Команда запуска: выполняем скрипт через uv run
CMD ["uv", "run", "gap_scanner.py"]
