# find-apartment

Поиск квартир в аренду в Минске (Ленинский район + Минск-Мир) на Kufar и Realt.by
с уведомлениями в Telegram. Найденные объявления сохраняются в SQLite (`listings.db`),
а новые (ещё не виденные) — присылаются в чат. Фильтры по цене и количеству комнат
можно менять прямо командами в Telegram.

## Компоненты

- `find_apartments.py` — поиск объявлений, сохранение в SQLite, рассылка новых в Telegram.
  Запускается по расписанию (в Docker — раз в 30 минут).
- `telegram_command_watcher.py` — слушает Telegram (long polling) и мгновенно
  применяет команды `цена <min> <max>` / `комнаты <list>`.
- `check_telegram_commands.py` — разовая (не long-polling) проверка новых команд;
  используется `telegram_command_watcher.py` и может запускаться отдельно.

## Конфигурация

Настройки лежат в `config.yaml` в корне проекта (создаётся автоматически при
первом запуске, если его нет):

```yaml
min_price: 500          # фильтр поиска, BYN/мес
max_price: 1300
rooms: "2,3"             # список количества комнат через запятую
bot_token: "ТОКЕН_БОТА"  # без bot_token/chat_id уведомления в Telegram просто не отправляются
chat_id: 123456789
telegram_offset: 0       # служебное: курсор Telegram getUpdates
```

Задавать нужно обычно только `bot_token`/`chat_id` (и при желании стартовые
`min_price`/`max_price`/`rooms`) — остальные поля управляются приложением само.

`watcher` — единственный писатель этого файла (настройки меняются командами в
Telegram, курсор обновляется на каждый опрос). Каждое обновление
(`ConfigStore.update()` в `find_apartments.py`) делает это под эксклюзивной
файловой блокировкой (`flock`) и сохраняет только изменённые поля — на случай,
если `check_telegram_commands.py` когда-нибудь запустят как отдельный cron
рядом с `watcher`, конкурентные записи не затрут друг друга.

## Результаты поиска

Каждое найденное объявление (полные данные — цена, адрес, комнаты, описание,
фото и т.д.) сохраняется в SQLite-базе `listings.db`, по одной строке на
объявление (`ListingsStore` в `find_apartments.py`). Эта же таблица используется
для дедупликации рассылки в Telegram: объявление, уже попавшее в базу, повторно
не отправляется. Сохранение в `listings.db` происходит при каждом прогоне
поиска независимо от того, настроен Telegram или нет.

## Запуск через Docker (рекомендуется)

Требуется установленный [Docker](https://www.docker.com/) с Docker Compose.

```bash
docker compose up -d --build
```

Поднимутся два сервиса:
- `finder` — поиск раз в 30 минут;
- `watcher` — слушает команды в Telegram.

Полезные команды:

```bash
docker compose logs -f          # логи обоих сервисов
docker compose logs -f finder   # логи только поиска
docker compose ps               # статус контейнеров
docker compose down             # остановить всё
```

`config.yaml` и `listings.db` лежат в примонтированной директории проекта и
переживают перезапуск/пересборку контейнеров.

### Команды в Telegram

Прямо в чате с ботом:

- `цена 600 1200` — задать диапазон цен (BYN/мес).
- `комнаты 1,2,3` — задать список количества комнат.

Изменения применятся на следующем прогоне поиска (до 30 минут).

## Проверки (тесты, линт, типы) через Docker

Отдельный сервис `checks` собирает код с dev-зависимостями и одной командой
прогоняет форматтер, линтер, проверку типов и тесты:

```bash
docker compose run --rm checks
```

Под капотом: `ruff format --check . && ruff check . && mypy && pytest -q`.

## Запуск без Docker

Требуется Python 3.12+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # requirements.txt + pytest/ruff/mypy

python find_apartments.py             # разовый поиск
python telegram_command_watcher.py    # слушать команды в Telegram

ruff format .                         # автоформатирование
ruff check --fix .                    # линт с автофиксом
mypy                                  # проверка типов
pytest                                # тесты
```

## Структура проекта

```
find_apartments.py           # скрапинг Kufar/Realt.by, ConfigStore, ListingsStore, Telegram-уведомления
check_telegram_commands.py   # разовая проверка команд в Telegram
telegram_command_watcher.py  # long-polling для мгновенной реакции на команды
config.yaml                  # настройки + Telegram-креды (создаётся автоматически)
listings.db                  # SQLite: полные данные найденных объявлений (создаётся автоматически)
tests/                       # unit-тесты (pytest)
Dockerfile                   # образ для finder/watcher
Dockerfile.dev               # образ с dev-зависимостями для сервиса checks
docker-compose.yml           # сервисы finder, watcher, checks
pyproject.toml               # конфиг ruff и mypy
```

## Лицензия

[MIT](LICENSE)
