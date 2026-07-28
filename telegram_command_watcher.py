#!/usr/bin/env python3
"""Long-poll Telegram for commands and react instantly (seconds, not minutes).

Uses Telegram's own long-polling (getUpdates with timeout=25): each request
blocks server-side until a new message arrives or 25s pass, so this reacts
immediately without hammering the API on a fixed interval.
"""

import time
from typing import NoReturn

import requests

from check_telegram_commands import CommandProcessor, OffsetStore
from find_apartments import SettingsStore, TelegramClient


class CommandWatcher:
    def __init__(
        self,
        client: TelegramClient,
        settings_store: SettingsStore,
        offset_store: OffsetStore,
        processor: CommandProcessor,
    ) -> None:
        self.client = client
        self.settings_store = settings_store
        self.offset_store = offset_store
        self.processor = processor

    def run(self) -> NoReturn:
        offset = self.offset_store.load()
        print("Watching for Telegram commands...", flush=True)

        while True:
            try:
                resp = requests.get(
                    f"https://api.telegram.org/bot{self.client.token}/getUpdates",
                    params={"offset": offset, "timeout": 25},
                    timeout=35,
                )
                resp.raise_for_status()
                updates = resp.json().get("result", [])
            except Exception as e:
                print(f"Poll error: {e}", flush=True)
                time.sleep(5)
                continue

            if not updates:
                continue

            settings = self.settings_store.load()
            changed = False
            for update in updates:
                offset = max(offset, update["update_id"] + 1)
                if self.processor.process(update, settings):
                    changed = True

            self.offset_store.save(offset)
            if changed:
                self.settings_store.save(settings)
                print(f"Settings updated: {settings}", flush=True)


def main() -> NoReturn:
    client = TelegramClient.from_config()
    settings_store = SettingsStore()
    offset_store = OffsetStore()
    processor = CommandProcessor(client)
    CommandWatcher(client, settings_store, offset_store, processor).run()


if __name__ == "__main__":
    main()
