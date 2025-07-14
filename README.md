# momentum-strategy-cameron
Полный русскоязычный разбор и практическая реализация моментум-стратегии Росса Камерона: документация, скрипты для сканеров и бэктесты, а также свежая аналитика её эффективности.

How to Day Trade: The Plain Truth Kindle Edition by Ross Cameron (Author)
https://www.amazon.com/How-Day-Trade-Plain-Truth-ebook/dp/B0CLKYY4BD/ref=zg_bs_g_154898011_d_sccl_1/133-3119227-0241521?psc=1

Работа с бэкглогом
https://dzen.ru/a/aGtzHttHxjW4hAfA

Инициализация бэклога
0. curl -fsSL https://bun.com/install | bash 
1. bun init -y
2. bun add -g backlog.md
3. backlog init

# Добавляем первые задачи и проверяем борд
backlog task create "Подготовить README"
backlog task create "Добавить стратегию Gap-and-Go" -s "In Progress"

# Смотрим терминальный Kanban
backlog board view          # интерактивный TUI
# или web-интерфейс
backlog browser --no-open   # откроется на http://localhost:6420