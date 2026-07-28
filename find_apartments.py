#!/usr/bin/env python3
"""Search Kufar and Realt for Minsk rentals (Leninsky district + Minsk-Mir) in a price band, persist to SQLite.

Usage: python find_apartments.py [--min PRICE] [--max PRICE]
"""

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any, ClassVar

import requests
import yaml

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.yaml")
LISTINGS_DB_PATH = os.path.join(SCRIPT_DIR, "listings.db")
NO_PHOTO_PATH = os.path.join(SCRIPT_DIR, "no_photo.png")

STREET_TYPE = (
    r"(?:ул(?:ица)?|просп(?:ект)?|пер(?:еулок)?|тракт|б(?:ул(?:ьвар)?|-р)|наб(?:ережная)?|пл(?:ощадь)?|ш(?:оссе)?|пр-т)"
)
ADDRESS_RE = re.compile(r"([а-яё\-]+)\s+" + STREET_TYPE + r"\.?,?\s*(\d+[а-яёa-z\d/]*)", re.IGNORECASE)

# A listing: source, price_byn, rooms, area_m2, floor, floors_total, address,
# lease_term, description, link, updated, images, plus scraper-internal
# bookkeeping (e.g. "_kufar_ad_id", "_realt_code"). Too loosely/dynamically
# shaped for a TypedDict to pull its weight, so this is just a readability alias.
Row = dict[str, Any]


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def clean_description(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class ConfigStore:
    """Read/write access to the single shared config.yaml file: user-adjustable
    search settings, Telegram credentials, and the command-poll offset.

    finder only ever reads this file; the watcher process is the sole writer
    (command-driven settings changes, Telegram poll offset). Every read and
    write is still wrapped in an flock() lock so a stray concurrent writer
    (e.g. check_telegram_commands.py invoked by cron alongside the watcher)
    can't clobber another writer's update mid-flight.
    """

    DEFAULTS: ClassVar[dict[str, Any]] = {
        "min_price": 500,
        "max_price": 1300,
        "rooms": "2,3",
        "bot_token": None,
        "chat_id": None,
        "telegram_offset": 0,
        "results_initialized": False,
    }

    def __init__(self, path: str = CONFIG_PATH) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_SH)
                try:
                    raw = f.read()
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
        except FileNotFoundError:
            return dict(self.DEFAULTS)
        return {**self.DEFAULTS, **(yaml.safe_load(raw) or {})}

    def update(self, **changes: Any) -> dict[str, Any]:
        """Atomically merge `changes` into the stored config and persist it."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        with os.fdopen(fd, "r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                data = {**(yaml.safe_load(f.read()) or {}), **changes}
                f.seek(0)
                f.truncate()
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return {**self.DEFAULTS, **data}


class ListingsStore:
    """Persistent SQLite record of every listing find_apartments.py has ever
    found, keyed by link, holding the full listing data (not just the link)
    so it's a queryable history. Also doubles as the Telegram notification
    dedup: a link already in the table has already been reported.
    """

    def __init__(self, path: str = LISTINGS_DB_PATH) -> None:
        self.path = path
        with self._connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS listings ("
                "link TEXT PRIMARY KEY, source TEXT, price_byn REAL, rooms TEXT, "
                "area_m2 TEXT, floor TEXT, floors_total TEXT, address TEXT, "
                "lease_term TEXT, description TEXT, photo TEXT, updated TEXT, images TEXT, "
                "first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL)"
            )

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def seen_links(self) -> set[str]:
        with self._connection() as conn:
            rows = conn.execute("SELECT link FROM listings").fetchall()
        return {link for (link,) in rows}

    def save(self, rows: list[Row]) -> None:
        """Upsert full listing data for every row that has a link.

        first_seen_at is preserved across re-runs (omitted from the UPDATE
        clause); last_seen_at is always bumped to now.
        """
        now = datetime.datetime.now().isoformat(timespec="seconds")
        values = [
            (
                row["link"],
                row.get("source"),
                row.get("price_byn"),
                row.get("rooms"),
                row.get("area_m2"),
                row.get("floor"),
                row.get("floors_total"),
                row.get("address"),
                row.get("lease_term"),
                row.get("description"),
                (row.get("images") or [None])[0],
                row.get("updated"),
                json.dumps(row.get("images") or []),
                now,
                now,
            )
            for row in rows
            if row.get("link")
        ]
        with self._connection() as conn:
            conn.executemany(
                "INSERT INTO listings (link, source, price_byn, rooms, area_m2, floor, "
                "floors_total, address, lease_term, description, photo, updated, images, "
                "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(link) DO UPDATE SET "
                "source=excluded.source, price_byn=excluded.price_byn, rooms=excluded.rooms, "
                "area_m2=excluded.area_m2, floor=excluded.floor, floors_total=excluded.floors_total, "
                "address=excluded.address, lease_term=excluded.lease_term, "
                "description=excluded.description, photo=excluded.photo, updated=excluded.updated, "
                "images=excluded.images, last_seen_at=excluded.last_seen_at",
                values,
            )


class SettingsStore:
    """Loads/saves the user-adjustable search settings, stored in config.yaml."""

    KEYS: ClassVar[tuple[str, ...]] = ("min_price", "max_price", "rooms")

    def __init__(self, config_store: "ConfigStore | None" = None) -> None:
        self.config_store = config_store or ConfigStore()

    def load(self) -> dict[str, Any]:
        config = self.config_store.load()
        return {k: config[k] for k in self.KEYS}

    def save(self, settings: dict[str, Any]) -> None:
        self.config_store.update(**{k: settings[k] for k in self.KEYS})


class TelegramNotConfigured(Exception):
    """Raised when config.yaml has no bot_token/chat_id set yet."""


class TelegramClient:
    """Thin wrapper around the subset of the Telegram Bot API this project uses."""

    def __init__(self, token: str, chat_id: int) -> None:
        self.token = token
        self.chat_id = chat_id

    @classmethod
    def from_config(cls, config_store: "ConfigStore | None" = None) -> "TelegramClient":
        config = (config_store or ConfigStore()).load()
        token, chat_id = config.get("bot_token"), config.get("chat_id")
        if not token or not chat_id:
            raise TelegramNotConfigured
        return cls(token, chat_id)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send_message(self, text: str) -> None:
        data = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            }
        ).encode()
        req = urllib.request.Request(self._url("sendMessage"), data=data, method="POST")
        urllib.request.urlopen(req, timeout=15)

    def send_photo(self, photo_url: str, caption: str | None = None) -> None:
        data: dict[str, Any] = {"chat_id": self.chat_id, "photo": photo_url}
        if caption:
            data["caption"] = caption
        req = urllib.request.Request(self._url("sendPhoto"), data=urllib.parse.urlencode(data).encode(), method="POST")
        urllib.request.urlopen(req, timeout=15)

    def send_media_group(self, image_urls: list[str], caption: str | None = None) -> None:
        # sendMediaGroup allows 2-10 items; only the first item's caption (if any) is shown by Telegram clients
        media: list[dict[str, str]] = [{"type": "photo", "media": url} for url in image_urls[:10]]
        if caption:
            media[0]["caption"] = caption
        data = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "media": json.dumps(media),
            }
        ).encode()
        req = urllib.request.Request(self._url("sendMediaGroup"), data=data, method="POST")
        urllib.request.urlopen(req, timeout=20)

    def send_photo_file(self, file_path: str, caption: str | None = None) -> None:
        data: dict[str, Any] = {"chat_id": self.chat_id}
        if caption:
            data["caption"] = caption
        with open(file_path, "rb") as f:
            resp = requests.post(self._url("sendPhoto"), data=data, files={"photo": f}, timeout=15)
        resp.raise_for_status()


class Scraper(ABC):
    source_name: ClassVar[str]

    @abstractmethod
    def fetch(self, price_min: float, price_max: float) -> list[Row]:
        """Return a list of row dicts for listings priced in [price_min, price_max]."""


class KufarScraper(Scraper):
    source_name = "kufar.by"

    API_URL = "https://api.kufar.by/search-api/v1/search/rendered-paginated"
    DETAIL_URL = "https://re.kufar.by/vi/{ad_id}"
    # Leninsky is an official city district (coder_district); Minsk-Mir is a
    # microdistrict (re_district) within region 7 (Minsk)
    AREA_PARAMS = [
        "gtsy=country-belarus~province-minsk~locality-minsk~coder_district-27",
        "gtsy=country-belarus~province-minsk~locality-minsk&rgn=7&red=v.or:170",
    ]

    def fetch(self, price_min: float, price_max: float) -> list[Row]:
        rows: list[Row] = []
        seen_ids: set[Any] = set()
        for area_params in self.AREA_PARAMS:
            url = (
                f"{self.API_URL}?cat=1010&cur=BYN&{area_params}&typ=let"
                f"&prc=r:{int(price_min * 100)},{int(price_max * 100)}&size=100"
            )
            data = json.loads(http_get(url))
            for ad in data.get("ads", []):
                ad_id = ad.get("ad_id")
                if ad_id in seen_ids:
                    continue
                seen_ids.add(ad_id)
                params = {p.get("p"): p.get("v") for p in ad.get("ad_parameters", [])}
                labels = {p.get("p"): p.get("vl") for p in ad.get("ad_parameters", [])}
                rooms = params.get("rooms")
                area = params.get("size")
                floor = params.get("floor")
                floor = floor[0] if isinstance(floor, list) and floor else floor
                floors_total = params.get("re_number_floors")
                floors_total = floors_total[0] if isinstance(floors_total, list) and floors_total else floors_total
                subject = ad.get("subject") or ""
                # prefer a street name embedded in the title (more specific, matches realt.by
                # for dedupe) over the generic region/district fallback
                address = (
                    params.get("address")
                    or (subject if ListingDeduper.address_key(subject) else None)
                    or self._fallback_address(labels)
                    or subject
                )
                list_time = ad.get("list_time", "")[:10]
                images = [f"https://rms.kufar.by/v1/list_thumbs_2x/{im['path']}" for im in (ad.get("images") or [])]
                rows.append(
                    {
                        "source": self.source_name,
                        "price_byn": round(int(ad.get("price_byn", 0)) / 100, 2),
                        "rooms": rooms,
                        "area_m2": area,
                        "floor": floor,
                        "floors_total": floors_total,
                        "address": address,
                        "lease_term": "не указан",  # kufar exposes no lease-duration field for apartments
                        "link": ad.get("ad_link"),
                        "updated": list_time,
                        "images": images,
                        "description": clean_description(ad.get("body_short")),
                        "_kufar_ad_id": ad_id,
                    }
                )
        return rows

    @staticmethod
    def _fallback_address(labels: dict[str, Any]) -> str | None:
        """Build a "Минск, Ленинский р-н, Серебрянка, м. Малиновка"-style address from
        region/district/microdistrict/metro labels, for private listings that hide the
        exact street address."""
        parts = []
        if labels.get("region"):
            parts.append(labels["region"])
        if labels.get("area"):
            parts.append(f"{labels['area']} р-н")
        if labels.get("re_district"):
            parts.append(labels["re_district"])
        metro = labels.get("metro")
        if metro:
            metro = metro[0] if isinstance(metro, list) else metro
            parts.append(f"м. {metro}")
        return ", ".join(parts) if parts else None

    def fetch_full_description(self, ad_id: int) -> str | None:
        """kufar's search API only returns a 150-char "body_short" preview; the full
        text only lives on the ad's own detail page."""
        html = http_get(self.DETAIL_URL.format(ad_id=ad_id)).decode("utf-8", errors="replace")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            return None
        data = json.loads(m.group(1))
        body = data["props"]["initialState"]["adView"]["data"].get("body")
        return clean_description(body) if body else None


class RealtScraper(Scraper):
    source_name = "realt.by"

    URLS = [
        "https://realt.by/rent/flat-for-long/minsk/leninskij-rajon/",
        "https://realt.by/rent/flat-for-long/minsk/minsk-mir-mk-r-n/",
    ]
    DETAIL_URL = "https://realt.by/rent-flat-for-long/object/{code}/"
    BYN_CURRENCY_CODE = "933"
    # realt.by's own leasePeriod labels; only these count as "at least a year"
    # (999 = "Длительный" / indefinite long-term)
    LONG_TERM_LABELS: ClassVar[set[str]] = {"Год", "Длительный"}

    def fetch(self, price_min: float, price_max: float) -> list[Row]:
        rows: list[Row] = []
        seen_codes: set[Any] = set()
        for base_url in self.URLS:
            page = 1
            while True:
                url = base_url if page == 1 else f"{base_url}?page={page}"
                html = http_get(url).decode("utf-8", errors="replace")
                m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
                if not m:
                    break
                data = json.loads(m.group(1))
                pp = data["props"]["pageProps"]
                objects = pp.get("objects") or []
                pagination = pp.get("pagination") or {}
                for o in objects:
                    code = o.get("code")
                    if code in seen_codes:
                        continue
                    price_byn = (o.get("priceRates") or {}).get(self.BYN_CURRENCY_CODE)
                    if price_byn is None or not (price_min <= price_byn <= price_max):
                        continue
                    seen_codes.add(code)
                    rows.append(
                        {
                            "source": self.source_name,
                            "price_byn": round(price_byn, 2),
                            "rooms": o.get("rooms"),
                            "area_m2": o.get("areaTotal"),
                            "floor": o.get("storey"),
                            "floors_total": o.get("storeys"),
                            "address": o.get("address"),
                            "lease_term": None,
                            "link": f"https://realt.by/rent-flat-for-long/object/{code}/" if code else None,
                            "updated": (o.get("updatedAt") or "")[:10],
                            "images": list(o.get("images") or []),
                            "description": clean_description(o.get("description")),
                            "_realt_code": code,
                        }
                    )
                total = pagination.get("totalCount", 0)
                page_size = pagination.get("pageSize", 30)
                if page * page_size >= total:
                    break
                page += 1
        return rows

    def fetch_lease_term(self, code: str) -> str | None:
        """realt.by only exposes leasePeriod on the listing's detail page, not in search results."""
        html = http_get(self.DETAIL_URL.format(code=code)).decode("utf-8", errors="replace")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            return None
        data = json.loads(m.group(1))
        return data["props"]["pageProps"]["object"].get("leasePeriod")


class ListingDeduper:
    """Merges listings for the same street+house across sources."""

    @staticmethod
    def address_key(address: str | None) -> str | None:
        """Street name + house number, e.g. 'Физкультурная ул., 14' -> 'физкультурная 14'.

        Returns None when no street+number pattern is found (generic titles like
        'Аренда' or 'Квартиры') so such rows are never merged with each other.
        """
        if not address:
            return None
        m = ADDRESS_RE.search(address.lower())
        if not m:
            return None
        return f"{m.group(1)} {m.group(2)}".replace("ё", "е")

    def dedupe(self, rows: list[Row]) -> list[Row]:
        merged: dict[str, Row] = {}
        order: list[str] = []
        for i, row in enumerate(rows):
            key = self.address_key(row.get("address")) or f"__no_address_{i}"
            if key not in merged:
                entry = dict(row)
                entry["_sources"] = [row["source"]]
                entry["_links"] = [row["link"]]
                merged[key] = entry
                order.append(key)
            else:
                entry = merged[key]
                entry["_sources"].append(row["source"])
                entry["_links"].append(row["link"])
                for f in ("rooms", "area_m2", "floor", "floors_total"):
                    if not entry.get(f) and row.get(f):
                        entry[f] = row[f]
                entry["images"] = list(dict.fromkeys((entry.get("images") or []) + (row.get("images") or [])))
                if len(row.get("description") or "") > len(entry.get("description") or ""):
                    entry["description"] = row["description"]
                if entry.get("lease_term") in (None, "не указан") and row.get("lease_term") not in (None, "не указан"):
                    entry["lease_term"] = row["lease_term"]
                if row.get("price_byn") is not None and (
                    entry.get("price_byn") is None or row["price_byn"] < entry["price_byn"]
                ):
                    entry["price_byn"] = row["price_byn"]
                if row.get("updated") and row["updated"] > (entry.get("updated") or ""):
                    entry["updated"] = row["updated"]

        result = []
        for key in order:
            entry = merged[key]
            entry["source"] = ", ".join(dict.fromkeys(entry.pop("_sources")))
            entry["link"] = "; ".join(dict.fromkeys(entry.pop("_links")))
            result.append(entry)
        return result


def format_telegram_message(row: Row) -> str:
    area = row.get("area_m2")
    floor = row.get("floor")
    floors_total = row.get("floors_total")
    floor_str = f"{floor}/{floors_total}" if floor and floors_total else "—"
    lease_term = row.get("lease_term")
    lines = [
        f"🆕🏠 Новая квартира: 💰 {row.get('price_byn')} BYN/мес",
        f"📐 {area} м² · 🏢 этаж {floor_str}",
        f"📍 {row.get('address')}",
    ]
    if lease_term:
        lines.append(f"📅 Срок аренды: {lease_term}")
    description = row.get("description")
    if description:
        lines.append(f"📝 {description}")
    lines.append(f"🔗 {row.get('link')}")
    return "\n".join(lines)


class ListingNotifier:
    """Sends Telegram messages for listings not seen in a previous run."""

    def __init__(
        self,
        config_store: "ConfigStore | None" = None,
        listings_store: "ListingsStore | None" = None,
        no_photo_path: str = NO_PHOTO_PATH,
    ) -> None:
        self.config_store = config_store or ConfigStore()
        self.listings_store = listings_store or ListingsStore()
        self.no_photo_path = no_photo_path

    def notify(self, rows: list[Row], notify_all: bool = False) -> int | None:
        """Persists every row into ListingsStore - this is the app's sole
        "save the results" step and happens regardless of Telegram config,
        same as the old Excel export used to.

        On the very first run (no results recorded yet) nothing is sent — it
        just establishes the baseline, so configuring Telegram later doesn't
        dump the entire accumulated history into the chat at once. Pass
        notify_all=True to send every current listing regardless of what was
        seen before.
        """
        is_first_run = not self.config_store.load()["results_initialized"]
        seen = self.listings_store.seen_links()
        new_rows = list(rows) if notify_all else [r for r in rows if r.get("link") and r["link"] not in seen]

        self.listings_store.save(rows)
        self.config_store.update(results_initialized=True)

        try:
            client = TelegramClient.from_config(self.config_store)
        except TelegramNotConfigured:
            return None

        if is_first_run and not notify_all:
            return 0

        sent = 0
        for i, row in enumerate(new_rows):
            if i > 0:
                time.sleep(2)  # keep a clear gap so consecutive listings/albums don't blur together
            text = format_telegram_message(row)
            images = row.get("images") or []
            try:
                if len(images) >= 2:
                    client.send_media_group(images, text)
                elif len(images) == 1:
                    client.send_photo(images[0], text)
                elif os.path.exists(self.no_photo_path):
                    client.send_photo_file(self.no_photo_path, text)
                else:
                    client.send_message(text)
                sent += 1
            except Exception as e:
                print(f"Telegram send failed for {row.get('link')}: {e}", file=sys.stderr)
                try:
                    client.send_message(text)
                    sent += 1
                except Exception as e2:
                    print(f"Telegram fallback text also failed for {row.get('link')}: {e2}", file=sys.stderr)
        return sent


class ApartmentFinder:
    def __init__(self) -> None:
        self.settings_store = SettingsStore()
        self.kufar = KufarScraper()
        self.realt = RealtScraper()
        self.scrapers: list[Scraper] = [self.kufar, self.realt]
        self.deduper = ListingDeduper()
        self.notifier = ListingNotifier()

    def run(self, args: argparse.Namespace) -> None:
        allowed_rooms = {r.strip() for r in args.rooms.split(",") if r.strip()}

        all_rows: list[Row] = []
        errors: list[str] = []
        for scraper in self.scrapers:
            try:
                all_rows.extend(scraper.fetch(args.min, args.max))
            except Exception as e:
                errors.append(f"{scraper.source_name}: {e}")

        if allowed_rooms:
            all_rows = [r for r in all_rows if str(r.get("rooms")) in allowed_rooms]

        if args.min_lease_year:
            all_rows = self._filter_min_lease_year(all_rows)

        self._fill_kufar_full_descriptions(all_rows)

        for row in all_rows:
            row.pop("_realt_code", None)
            row.pop("_kufar_ad_id", None)

        before = len(all_rows)
        all_rows = self.deduper.dedupe(all_rows)
        all_rows.sort(key=lambda r: (r["price_byn"] is None, r["price_byn"]))
        sent = self.notifier.notify(all_rows, notify_all=args.notify_all)

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"[{ts}] Saved {len(all_rows)} listings ({before - len(all_rows)} duplicates merged) "
            f"({args.min}-{args.max} BYN) to listings.db"
        )
        if sent is not None:
            print(f"Telegram: sent {sent} new listing(s)" if sent else "Telegram: no new listings to send")
        if errors:
            print("Errors:", "; ".join(errors), file=sys.stderr)

    def _filter_min_lease_year(self, rows: list[Row]) -> list[Row]:
        kept = []
        for row in rows:
            if row["source"] == self.kufar.source_name:
                # no lease-term data available from kufar.by; kept as-is (see printed note)
                kept.append(row)
                continue
            try:
                term = self.realt.fetch_lease_term(row["_realt_code"])
            except Exception:
                term = None
            row["lease_term"] = term or "не указан"
            if term is None or term in RealtScraper.LONG_TERM_LABELS:
                kept.append(row)
        return kept

    def _fill_kufar_full_descriptions(self, rows: list[Row]) -> None:
        for row in rows:
            if row["source"] != self.kufar.source_name or not row.get("_kufar_ad_id"):
                continue
            full_description = None
            for attempt in range(2):
                try:
                    full_description = self.kufar.fetch_full_description(row["_kufar_ad_id"])
                    break
                except Exception as e:
                    if attempt == 0:
                        time.sleep(1)
                    else:
                        print(f"Full description fetch failed for ad {row['_kufar_ad_id']}: {e}", file=sys.stderr)
            if full_description:
                row["description"] = full_description
            time.sleep(0.3)  # be polite to kufar's detail-page endpoint


def main() -> None:
    settings = SettingsStore().load()
    parser = argparse.ArgumentParser()
    parser.add_argument("--min", type=float, default=settings["min_price"])
    parser.add_argument("--max", type=float, default=settings["max_price"])
    parser.add_argument("--rooms", default=settings["rooms"], help="Comma-separated room counts to keep, e.g. 2,3")
    parser.add_argument(
        "--any-lease-term",
        action="store_true",
        help="Don't filter out realt.by listings with a lease term under a year",
    )
    parser.add_argument(
        "--notify-all",
        action="store_true",
        help="Send every current listing to Telegram, not just new ones",
    )
    args = parser.parse_args()
    args.min_lease_year = not args.any_lease_term

    ApartmentFinder().run(args)


if __name__ == "__main__":
    main()
