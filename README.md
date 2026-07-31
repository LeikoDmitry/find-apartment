<p align="center">
  <img src="banner.svg" alt="find-apartment — поиск квартир в аренду в Минске с уведомлениями в Telegram" width="100%">
</p>

# find-apartment

[![Tests](https://github.com/LeikoDmitry/find-apartment/actions/workflows/tests.yml/badge.svg)](https://github.com/LeikoDmitry/find-apartment/actions/workflows/tests.yml)
[![Publish Docker image](https://github.com/LeikoDmitry/find-apartment/actions/workflows/publish.yml/badge.svg)](https://github.com/LeikoDmitry/find-apartment/actions/workflows/publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)

Поиск квартир в аренду в Минске (Ленинский район + Минск-Мир) на Kufar и Realt.by
с уведомлениями в Telegram. Найденные объявления сохраняются в SQLite (`listings.db`),
а новые (ещё не виденные) — присылаются в чат вместе с фото; если у объявления есть
координаты, они добавляются отдельной строкой в текст. Фильтры по цене и количеству
комнат можно менять прямо командами в Telegram.

## Компоненты

- `find_apartments.py` — поиск объявлений, сохранение в SQLite, рассылка новых в Telegram.
  Запускается по расписанию (в Docker — раз в 10 минут).
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

`docker-compose.yml` ссылается на готовый образ
`ghcr.io/leikodmitry/find-apartment:1.0.2` (multi-arch: amd64 + arm64), но
у сервисов также указан `build: .`, так что можно либо собрать локально:

```bash
docker compose up -d --build
```

либо забрать уже собранный образ без локальной сборки. Пакет приватный
(наследует видимость репозитория), поэтому сначала логин в GHCR:

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u <github-username> --password-stdin
docker compose pull finder watcher
docker compose up -d
```

Поднимутся два сервиса:
- `finder` — поиск раз в 10 минут;
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

### Ручной запуск проверки объявлений

Не дожидаясь расписания (раз в 10 минут), можно запустить поиск прямо сейчас
в уже работающем контейнере `finder`:

```bash
docker compose run --rm finder python find_apartments.py
```

Полезные флаги:
- `--notify-all` — принудительно переслать в Telegram все текущие объявления,
  а не только новые.
- `--min 600 --max 1200`, `--rooms 1,2` — разово переопределить фильтры без
  изменения `config.yaml`.

Без Docker — то же самое, но `python find_apartments.py` в активированном venv
(см. [«Запуск без Docker»](#запуск-без-docker)).

### Команды в Telegram

Прямо в чате с ботом:

- `цена 600 1200` — задать диапазон цен (BYN/мес).
- `комнаты 1,2,3` — задать список количества комнат.

Изменения применятся на следующем прогоне поиска (до 10 минут).

## Проверки (тесты, линт, типы) через Docker

Отдельный сервис `checks` собирает код с dev-зависимостями и одной командой
прогоняет форматтер, линтер, проверку типов и тесты:

```bash
docker compose run --rm checks
```

Под капотом: `ruff format --check . && ruff check . && mypy && pytest -q`.

## Релизы и Docker-образ

При каждом пуше в `master` воркфлоу `.github/workflows/release.yml` сам
поднимает patch-версию (`vX.Y.Z` → `vX.Y.Z+1`), создаёт и пушит тег и
публикует GitHub Release с автосгенерированным описанием (список
смёрженных PR/коммитов). Пуш нового тега в свою очередь запускает
`.github/workflows/publish.yml`, который собирает и публикует образ в GitHub
Container Registry — `ghcr.io/leikodmitry/find-apartment:X.Y.Z` (и
`:latest`), multi-arch (amd64 + arm64). Ручной запуск публикации
(`workflow_dispatch` в Actions, поле `ref`) остаётся на случай тегов,
созданных не через этот воркфлоу.

`docker-compose.yml` закреплён на конкретной версии (не `:latest`) —
после нового релиза тег в `image:` нужно поднять руками.

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
find_apartments.py             # скрапинг Kufar/Realt.by, ConfigStore, ListingsStore, Telegram-уведомления
check_telegram_commands.py     # разовая проверка команд в Telegram
telegram_command_watcher.py    # long-polling для мгновенной реакции на команды
config.yaml                    # настройки + Telegram-креды (создаётся автоматически)
listings.db                    # SQLite: полные данные найденных объявлений (создаётся автоматически)
tests/                         # unit-тесты (pytest)
banner.svg                     # шапка этого README
Dockerfile                     # образ для finder/watcher (публикуется в GHCR)
Dockerfile.dev                 # образ с dev-зависимостями для сервиса checks
docker-compose.yml             # сервисы finder, watcher, checks
pyproject.toml                 # конфиг ruff и mypy
.github/workflows/tests.yml    # CI: ruff + mypy + pytest на push/PR
.github/workflows/publish.yml  # сборка и публикация образа в GHCR на тег vX.Y.Z
```

## Лицензия

[MIT](LICENSE)
