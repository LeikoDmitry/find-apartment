#!/usr/bin/env python3
"""Poll Telegram for price/room-filter commands and update config.yaml.

Commands (case-insensitive, Russian):
  "цена <min> <max>"   -> set price range, e.g. "цена 600 1200"
  "комнаты <list>"     -> set room-count filter, e.g. "комнаты 1,2,3"

Applied filters take effect on the next full search run (find_apartments.py),
not instantly - this script only updates config.yaml and acknowledges the
command back in the chat. For instant acknowledgement, run
telegram_command_watcher.py instead (long-polling loop).
"""

import re
from typing import Any

import requests

from find_apartments import ConfigStore, SettingsStore, TelegramClient, TelegramNotConfigured


class OffsetStore:
    def __init__(self, config_store: ConfigStore | None = None) -> None:
        self.config_store = config_store or ConfigStore()

    def load(self) -> int:
        return self.config_store.load()["telegram_offset"]

    def save(self, offset: int) -> None:
        self.config_store.update(telegram_offset=offset)


class CommandProcessor:
    """Applies recognized Telegram commands to a settings dict and acknowledges them in chat."""

    PRICE_RE = re.compile(r"^\s*цена\s+(\d+)\s+(\d+)\s*$", re.IGNORECASE)
    ROOMS_RE = re.compile(r"^\s*комнаты\s+([\d,\s]+)\s*$", re.IGNORECASE)

    def __init__(self, telegram_client: TelegramClient) -> None:
        self.telegram_client = telegram_client

    def process(self, update: dict[str, Any], settings: dict[str, Any]) -> bool:
        """Apply a recognized command from one Telegram update. Returns True if settings changed."""
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        if not text:
            return False

        m = self.PRICE_RE.match(text)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            settings["min_price"], settings["max_price"] = min(lo, hi), max(lo, hi)
            self.telegram_client.send_message(
                f"✅ Диапазон цен обновлён: {settings['min_price']}–{settings['max_price']} BYN.\n"
                f"Применится при следующем поиске (раз в 10 минут).",
            )
            return True

        m = self.ROOMS_RE.match(text)
        if m:
            rooms = ",".join(sorted({r.strip() for r in m.group(1).split(",") if r.strip()}))
            settings["rooms"] = rooms
            self.telegram_client.send_message(
                f"✅ Фильтр по комнатам обновлён: {rooms}.\nПрименится при следующем поиске (раз в 10 минут).",
            )
            return True

        return False


def main() -> None:
    try:
        client = TelegramClient.from_config()
    except TelegramNotConfigured:
        print("Telegram не настроен, проверять нечего.")
        return

    settings_store = SettingsStore()
    offset_store = OffsetStore()
    processor = CommandProcessor(client)

    offset = offset_store.load()
    resp = requests.get(
        f"https://api.telegram.org/bot{client.token}/getUpdates",
        params={"offset": offset, "timeout": 0},
        timeout=15,
    )
    resp.raise_for_status()
    updates = resp.json().get("result", [])
    if not updates:
        print("Новых сообщений нет.")
        return

    settings = settings_store.load()
    changed = False
    max_update_id = offset - 1

    for update in updates:
        max_update_id = max(max_update_id, update["update_id"])
        if processor.process(update, settings):
            changed = True

    if changed:
        settings_store.save(settings)
        print(f"Настройки обновлены: {settings}")
    else:
        print("Распознанных команд в новых сообщениях нет.")

    offset_store.save(max_update_id + 1)


if __name__ == "__main__":
    main()
