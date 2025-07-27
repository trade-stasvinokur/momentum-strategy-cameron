source .venv/bin/activate

1.	Берёт тикер из вашей SQLite-базы (таблица gap_records​— там уже сохраняются бумаги, найденные сканером гэпов).
2.	По каждому тикеру запрашивает минутные свечи через Tinkoff Invest API (точно так же, как в gap_scanner.py).
3.	Считает дневной VWAP и строит динамические уровни поддержки/сопротивления
\text{Support}=VWAP-\sigma,\;
\text{Resistance}=VWAP+\sigma,
где \sigma — стандартное отклонение (Price-VWAP) за сессию.
4.	Записывает результат в новую таблицу vwap_levels.