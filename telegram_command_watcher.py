#!/usr/bin/env python3
"""Long-poll Telegram for commands and react instantly (seconds, not minutes).

Uses Telegram's own long-polling (getUpdates with timeout=25): each request
blocks server-side until a new message arrives or 25s pass, so this reacts
immediately without hammering the API on a fixed interval.

Telegram serves only one getUpdates call per bot at a time, so this process
also holds an flock() on watcher.lock and refuses to start when another
watcher over the same project directory already has it.
"""

import fcntl
import os
import sys
import time
from typing import NoReturn, TextIO

import requests

from check_telegram_commands import CommandProcessor, OffsetStore
from find_apartments import SCRIPT_DIR, SettingsStore, TelegramClient

LOCK_PATH = os.path.join(SCRIPT_DIR, "watcher.lock")

POLL_ERROR_BACKOFF_SECONDS = 5

# Telegram answers 409 when another getUpdates call for the same bot is already
# in flight, kicking whichever got there first. Two watchers on one token then
# steal the poll from each other indefinitely and commands arrive only by luck.
# Nothing here can resolve that - only shutting the duplicate down can - so back
# off long enough to keep the log readable until somebody does.
CONFLICT_BACKOFF_SECONDS = 60


def is_conflict(error: Exception) -> bool:
    """True for Telegram's 409, i.e. some other process is polling this same bot."""
    return isinstance(error, requests.HTTPError) and error.response is not None and error.response.status_code == 409


def acquire_single_instance_lock() -> TextIO:
    """Take the watcher lock, or exit(1) if another watcher already holds it.

    Returns the open handle: closing it - or merely dropping the last reference
    to it - releases the lock, so the caller must keep it alive for as long as
    it polls. The OS releases it when the process exits, however it exits.
    """
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    lock_file = os.fdopen(fd, "r+", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = lock_file.read().strip() or "неизвестен"
        lock_file.close()
        print(
            f"Watcher уже запущен (PID {holder}, блокировка {LOCK_PATH}).\n"
            "Второй экземпляр на том же токене выбивал бы первый из getUpdates - выхожу.",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1) from None
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(f"{os.getpid()}\n")
    lock_file.flush()
    return lock_file


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
        print("Слежу за командами в Telegram...", flush=True)

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
                if is_conflict(e):
                    print(
                        "Ошибка опроса: 409 Conflict - бота уже опрашивает другой процесс "
                        "(второй watcher или check_telegram_commands.py, возможно на другой машине). "
                        f"На один токен должен приходиться ровно один опрашивающий. "
                        f"Повтор через {CONFLICT_BACKOFF_SECONDS} с.",
                        flush=True,
                    )
                    time.sleep(CONFLICT_BACKOFF_SECONDS)
                else:
                    print(f"Ошибка опроса: {e}", flush=True)
                    time.sleep(POLL_ERROR_BACKOFF_SECONDS)
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
                print(f"Настройки обновлены: {settings}", flush=True)


def main() -> NoReturn:
    # Held in a local for the whole life of the process on purpose: letting the
    # handle go would close the file and hand the lock to a second watcher.
    _lock_file = acquire_single_instance_lock()
    client = TelegramClient.from_config()
    settings_store = SettingsStore()
    offset_store = OffsetStore()
    processor = CommandProcessor(client)
    CommandWatcher(client, settings_store, offset_store, processor).run()


if __name__ == "__main__":
    main()
