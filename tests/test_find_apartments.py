import json
import urllib.error
from argparse import Namespace
from email.message import Message
from unittest.mock import Mock

import pytest
import yaml

import find_apartments as fa

# --------------------------------------------------------------------------
# ConfigStore
# --------------------------------------------------------------------------


class TestConfigStore:
    def test_load_returns_defaults_when_file_missing(self, tmp_path):
        store = fa.ConfigStore(tmp_path / "missing.yaml")
        assert store.load() == fa.ConfigStore.DEFAULTS

    def test_load_merges_partial_config_with_defaults(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"min_price": 700}), encoding="utf-8")
        store = fa.ConfigStore(path)
        loaded = store.load()
        assert loaded["min_price"] == 700
        assert loaded["max_price"] == fa.ConfigStore.DEFAULTS["max_price"]

    def test_update_creates_file_and_persists_changes(self, tmp_path):
        path = tmp_path / "config.yaml"
        store = fa.ConfigStore(path)
        store.update(min_price=600, max_price=1200)
        assert store.load()["min_price"] == 600
        assert store.load()["max_price"] == 1200

    def test_sequential_updates_do_not_clobber_unrelated_keys(self, tmp_path):
        """watcher and a standalone check_telegram_commands.py cron job could both
        write this file; each update() must preserve keys the *other* one last wrote."""
        path = tmp_path / "config.yaml"
        store_a = fa.ConfigStore(path)
        store_b = fa.ConfigStore(path)

        store_a.update(rooms="1,2")
        store_b.update(min_price=600, telegram_offset=5)
        store_a.update(rooms="1,2,3")

        final = fa.ConfigStore(path).load()
        assert final["rooms"] == "1,2,3"
        assert final["min_price"] == 600
        assert final["telegram_offset"] == 5

    def test_update_returns_the_full_merged_config(self, tmp_path):
        store = fa.ConfigStore(tmp_path / "config.yaml")
        result = store.update(rooms="1,2")
        assert result["rooms"] == "1,2"
        assert result["max_price"] == fa.ConfigStore.DEFAULTS["max_price"]


# --------------------------------------------------------------------------
# SettingsStore
# --------------------------------------------------------------------------


class TestSettingsStore:
    def test_load_returns_defaults_when_file_missing(self, tmp_path):
        store = fa.SettingsStore(fa.ConfigStore(tmp_path / "missing.yaml"))
        assert store.load() == {"min_price": 500, "max_price": 1300, "rooms": "2,3"}

    def test_load_merges_partial_settings_with_defaults(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"min_price": 700}), encoding="utf-8")
        store = fa.SettingsStore(fa.ConfigStore(path))
        assert store.load() == {"min_price": 700, "max_price": 1300, "rooms": "2,3"}

    def test_save_writes_roundtrip_without_touching_other_config_keys(self, tmp_path):
        path = tmp_path / "config.yaml"
        config_store = fa.ConfigStore(path)
        config_store.update(bot_token="TOKEN", chat_id=42)
        store = fa.SettingsStore(config_store)

        store.save({"min_price": 600, "max_price": 1200, "rooms": "1,2"})

        assert store.load() == {"min_price": 600, "max_price": 1200, "rooms": "1,2"}
        assert config_store.load()["bot_token"] == "TOKEN"
        assert config_store.load()["chat_id"] == 42


# --------------------------------------------------------------------------
# TelegramClient
# --------------------------------------------------------------------------


class TestTelegramClient:
    def test_from_config_reads_token_and_chat_id(self, tmp_path):
        config_store = fa.ConfigStore(tmp_path / "config.yaml")
        config_store.update(bot_token="TOKEN", chat_id=42)
        client = fa.TelegramClient.from_config(config_store)
        assert client.token == "TOKEN"
        assert client.chat_id == 42

    def test_from_config_raises_when_not_configured(self, tmp_path):
        config_store = fa.ConfigStore(tmp_path / "config.yaml")
        with pytest.raises(fa.TelegramNotConfigured):
            fa.TelegramClient.from_config(config_store)

    def test_send_message_posts_expected_payload(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["data"] = req.data.decode()
            return Mock()

        monkeypatch.setattr(fa.urllib.request, "urlopen", fake_urlopen)
        client = fa.TelegramClient("TOKEN", 42)
        client.send_message("hello")

        assert captured["url"] == "https://api.telegram.org/botTOKEN/sendMessage"
        assert captured["method"] == "POST"
        assert "chat_id=42" in captured["data"]
        assert "text=hello" in captured["data"]
        assert "disable_web_page_preview=true" in captured["data"]

    def test_send_photo_includes_caption(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data.decode()
            return Mock()

        monkeypatch.setattr(fa.urllib.request, "urlopen", fake_urlopen)
        client = fa.TelegramClient("TOKEN", 42)
        client.send_photo("https://example.com/a.jpg", caption="caption text")

        assert captured["url"] == "https://api.telegram.org/botTOKEN/sendPhoto"
        assert "photo=https" in captured["data"]
        assert "caption=caption" in captured["data"]

    def test_send_media_group_caps_at_ten_and_captions_only_first(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["data"] = req.data.decode()
            return Mock()

        monkeypatch.setattr(fa.urllib.request, "urlopen", fake_urlopen)
        client = fa.TelegramClient("TOKEN", 42)
        images = [f"https://example.com/{i}.jpg" for i in range(12)]
        client.send_media_group(images, caption="caption text")

        media_json = json.loads(fa.urllib.parse.parse_qs(captured["data"])["media"][0])
        assert len(media_json) == 10
        assert media_json[0]["caption"] == "caption text"
        assert "caption" not in media_json[1]

    def test_send_photo_file_posts_multipart(self, monkeypatch, tmp_path):
        photo_path = tmp_path / "no_photo.png"
        photo_path.write_bytes(b"fake-image-bytes")
        captured = {}

        def fake_post(url, data=None, files=None, timeout=None):
            captured["url"] = url
            captured["data"] = data
            captured["files"] = files
            resp = Mock()
            resp.raise_for_status = Mock()
            return resp

        monkeypatch.setattr(fa.requests, "post", fake_post)
        client = fa.TelegramClient("TOKEN", 42)
        client.send_photo_file(str(photo_path), caption="caption text")

        assert captured["url"] == "https://api.telegram.org/botTOKEN/sendPhoto"
        assert captured["data"]["chat_id"] == 42
        assert captured["data"]["caption"] == "caption text"
        assert "photo" in captured["files"]


# --------------------------------------------------------------------------
# ListingDeduper
# --------------------------------------------------------------------------


class TestListingDeduper:
    def test_address_key_extracts_street_and_house_number(self):
        assert fa.ListingDeduper.address_key("Физкультурная ул., 14") == "физкультурная 14"

    def test_address_key_returns_none_for_generic_title(self):
        assert fa.ListingDeduper.address_key("Аренда") is None

    def test_address_key_returns_none_for_empty_input(self):
        assert fa.ListingDeduper.address_key(None) is None
        assert fa.ListingDeduper.address_key("") is None

    def test_dedupe_merges_rows_at_same_address(self):
        rows = [
            {
                "source": "kufar.by",
                "price_byn": 900.0,
                "rooms": None,
                "area_m2": None,
                "floor": None,
                "floors_total": None,
                "address": "Физкультурная ул., 14",
                "link": "https://kufar.by/1",
                "updated": "2026-07-01",
                "images": ["img1.jpg"],
                "description": "short",
                "latitude": None,
                "longitude": None,
            },
            {
                "source": "realt.by",
                "price_byn": 850.0,
                "rooms": "2",
                "area_m2": "45",
                "floor": "3",
                "floors_total": "9",
                "address": "Физкультурная ул., 14",
                "link": "https://realt.by/1",
                "updated": "2026-07-05",
                "images": ["img2.jpg"],
                "description": "a longer description",
                "latitude": 53.9006,
                "longitude": 27.559,
            },
        ]
        result = fa.ListingDeduper().dedupe(rows)

        assert len(result) == 1
        entry = result[0]
        assert entry["source"] == "kufar.by, realt.by"
        assert entry["link"] == "https://kufar.by/1; https://realt.by/1"
        assert entry["rooms"] == "2"
        assert entry["area_m2"] == "45"
        assert entry["floor"] == "3"
        assert entry["floors_total"] == "9"
        assert entry["latitude"] == 53.9006  # missing on the entry, filled in from the merged row
        assert entry["longitude"] == 27.559
        assert entry["images"] == ["img1.jpg", "img2.jpg"]
        assert entry["description"] == "a longer description"
        assert entry["price_byn"] == 850.0  # cheaper of the two wins
        assert entry["updated"] == "2026-07-05"  # most recent wins

    def test_dedupe_keeps_distinct_addresses_separate(self):
        rows = [
            {"source": "kufar.by", "price_byn": 900.0, "address": "Физкультурная ул., 14", "link": "l1"},
            {"source": "realt.by", "price_byn": 850.0, "address": "Немига ул., 5", "link": "l2"},
        ]
        result = fa.ListingDeduper().dedupe(rows)
        assert len(result) == 2

    def test_dedupe_never_merges_rows_without_a_parseable_address(self):
        rows = [
            {"source": "kufar.by", "price_byn": 900.0, "address": "Аренда", "link": "l1"},
            {"source": "realt.by", "price_byn": 850.0, "address": "Квартиры", "link": "l2"},
        ]
        result = fa.ListingDeduper().dedupe(rows)
        assert len(result) == 2


# --------------------------------------------------------------------------
# format_telegram_message
# --------------------------------------------------------------------------


class TestFormatTelegramMessage:
    def test_includes_floor_when_present(self):
        text = fa.format_telegram_message(
            {
                "price_byn": 800.0,
                "area_m2": "45",
                "floor": "3",
                "floors_total": "9",
                "address": "Физкультурная ул., 14",
                "link": "https://kufar.by/1",
            }
        )
        assert "🏢 этаж 3/9" in text
        assert "📍 Физкультурная ул., 14" in text
        assert "🔗 https://kufar.by/1" in text

    def test_uses_dash_when_floor_missing(self):
        text = fa.format_telegram_message(
            {
                "price_byn": 800.0,
                "area_m2": "45",
                "floor": None,
                "floors_total": None,
                "address": "где-то",
                "link": "l",
            }
        )
        assert "этаж —" in text

    def test_includes_description_when_present(self):
        text = fa.format_telegram_message(
            {
                "price_byn": 800.0,
                "area_m2": "45",
                "floor": 1,
                "floors_total": 5,
                "address": "a",
                "link": "l",
                "description": "хорошая квартира",
            }
        )
        assert "📝 хорошая квартира" in text

    def test_omits_description_when_absent(self):
        text = fa.format_telegram_message(
            {
                "price_byn": 800.0,
                "area_m2": "45",
                "floor": 1,
                "floors_total": 5,
                "address": "a",
                "link": "l",
            }
        )
        assert "📝" not in text

    def test_includes_coordinates_when_present(self):
        text = fa.format_telegram_message(
            {
                "price_byn": 800.0,
                "area_m2": "45",
                "floor": 1,
                "floors_total": 5,
                "address": "a",
                "link": "l",
                "latitude": 53.9006,
                "longitude": 27.559,
            }
        )
        assert "🗺 53.9006, 27.559" in text

    def test_omits_coordinates_when_absent(self):
        text = fa.format_telegram_message(
            {
                "price_byn": 800.0,
                "area_m2": "45",
                "floor": 1,
                "floors_total": 5,
                "address": "a",
                "link": "l",
            }
        )
        assert "🗺" not in text

    def test_max_length_leaves_short_text_untouched(self):
        row = {
            "price_byn": 800.0,
            "area_m2": "45",
            "floor": 1,
            "floors_total": 5,
            "address": "a",
            "link": "l",
            "description": "хорошая квартира",
        }
        assert fa.format_telegram_message(row, max_length=1024) == fa.format_telegram_message(row)

    def test_max_length_trims_long_description_but_keeps_header_and_link(self):
        row = {
            "price_byn": 800.0,
            "area_m2": "45",
            "floor": 1,
            "floors_total": 5,
            "address": "a",
            "link": "https://kufar.by/1",
            "description": "x" * 2000,
        }
        text = fa.format_telegram_message(row, max_length=fa.TELEGRAM_CAPTION_LIMIT)
        assert len(text) <= fa.TELEGRAM_CAPTION_LIMIT
        assert text.endswith("🔗 https://kufar.by/1")
        assert "🏢 этаж 1/5" in text
        assert text.count("x") < 2000


# --------------------------------------------------------------------------
# ListingNotifier
# --------------------------------------------------------------------------


def _row(link, images=None, source="kufar.by", address="Физкультурная ул., 14"):
    return {
        "source": source,
        "price_byn": 800.0,
        "rooms": "2",
        "area_m2": "45",
        "floor": "3",
        "floors_total": "9",
        "address": address,
        "description": "desc",
        "link": link,
        "updated": "2026-07-01",
        "images": images or [],
    }


class TestListingsStore:
    def test_seen_links_empty_for_fresh_store(self, tmp_path):
        store = fa.ListingsStore(tmp_path / "listings.db")
        assert store.seen_links([]) == set()

    def test_seen_links_returns_only_candidates_present_in_db(self, tmp_path):
        store = fa.ListingsStore(tmp_path / "listings.db")
        store.save([_row("l1"), _row("l2")])
        assert store.seen_links(["l1", "l3"]) == {"l1"}

    def test_stored_descriptions_empty_for_fresh_store(self, tmp_path):
        store = fa.ListingsStore(tmp_path / "listings.db")
        assert store.stored_descriptions() == {}

    def test_stored_descriptions_maps_link_to_description(self, tmp_path):
        store = fa.ListingsStore(tmp_path / "listings.db")
        store.save([dict(_row("l1"), description="полное описание"), dict(_row("l2"), description=None)])
        assert store.stored_descriptions() == {"l1": "полное описание"}

    def test_save_persists_full_listing_data(self, tmp_path):
        store = fa.ListingsStore(tmp_path / "listings.db")
        store.save([_row("l1", images=["a.jpg", "b.jpg"])])

        assert store.seen_links(["l1"]) == {"l1"}
        with store._connection() as conn:
            cursor = conn.execute("SELECT * FROM listings WHERE link = 'l1'")
            row = cursor.fetchone()
            columns = [d[0] for d in cursor.description]
        assert row is not None
        record = dict(zip(columns, row, strict=True))
        assert record["source"] == "kufar.by"
        assert record["price_byn"] == 800.0
        assert record["address"] == "Физкультурная ул., 14"
        assert record["photo"] == "a.jpg"
        assert json.loads(record["images"]) == ["a.jpg", "b.jpg"]
        assert record["first_seen_at"] == record["last_seen_at"]

    def test_save_ignores_rows_without_a_link(self, tmp_path):
        store = fa.ListingsStore(tmp_path / "listings.db")
        store.save([dict(_row("l1"), link=None)])
        assert store.seen_links(["l1"]) == set()

    def test_save_upserts_and_keeps_first_seen_at_but_bumps_last_seen_at(self, tmp_path, monkeypatch):
        store = fa.ListingsStore(tmp_path / "listings.db")

        class FakeDatetime(fa.datetime.datetime):
            _now = fa.datetime.datetime(2026, 1, 1, 10, 0, 0)

            @classmethod
            def now(cls, tz=None):
                return cls._now

        monkeypatch.setattr(fa.datetime, "datetime", FakeDatetime)
        store.save([_row("l1", images=["a.jpg"])])

        FakeDatetime._now = fa.datetime.datetime(2026, 1, 2, 12, 0, 0)
        store.save([dict(_row("l1", images=["a.jpg"]), price_byn=750.0)])

        with store._connection() as conn:
            row = conn.execute("SELECT price_byn, first_seen_at, last_seen_at FROM listings WHERE link='l1'").fetchone()
        price_byn, first_seen_at, last_seen_at = row
        assert price_byn == 750.0
        assert first_seen_at == "2026-01-01T10:00:00"
        assert last_seen_at == "2026-01-02T12:00:00"

    def test_all_listings_sorted_by_price_with_nulls_last(self, tmp_path):
        store = fa.ListingsStore(tmp_path / "listings.db")
        store.save(
            [
                dict(_row("cheap", address="Немига ул., 5"), price_byn=500.0),
                dict(_row("no_price", address="Аренда"), price_byn=None),
                dict(_row("expensive", address="Игуменский тракт, 20"), price_byn=1200.0),
            ]
        )

        listings = store.all_listings()

        assert [row["link"] for row in listings] == ["cheap", "expensive", "no_price"]
        assert listings[0]["price_byn"] == 500.0
        assert listings[0]["address"] == "Немига ул., 5"


class TestListingNotifier:
    def _notifier(self, tmp_path, configured=True):
        config_store = fa.ConfigStore(tmp_path / "config.yaml")
        if configured:
            config_store.update(bot_token="TOKEN", chat_id=42)
        listings_store = fa.ListingsStore(tmp_path / "listings.db")
        no_photo_path = tmp_path / "no_photo.png"
        return (
            fa.ListingNotifier(config_store, listings_store, no_photo_path),
            config_store,
            listings_store,
            no_photo_path,
        )

    def test_returns_none_when_telegram_not_configured_but_still_saves_results(self, tmp_path):
        notifier, config_store, listings_store, _ = self._notifier(tmp_path, configured=False)
        result = notifier.notify([_row("l1")])
        assert result is None
        # DB persistence is unconditional (mirrors the old Excel export behavior),
        # even though there's no Telegram to notify
        assert config_store.load()["results_initialized"] is True
        assert listings_store.seen_links(["l1"]) == {"l1"}

    def test_first_run_establishes_baseline_without_sending(self, tmp_path, monkeypatch):
        notifier, config_store, listings_store, _ = self._notifier(tmp_path)
        send_message = Mock()
        monkeypatch.setattr(fa.TelegramClient, "send_message", send_message)

        result = notifier.notify([_row("l1"), _row("l2")])

        assert result == 0
        send_message.assert_not_called()
        assert listings_store.seen_links(["l1", "l2"]) == {"l1", "l2"}
        assert config_store.load()["results_initialized"] is True

    def test_notify_all_sends_even_on_first_run(self, tmp_path, monkeypatch):
        notifier, _, _, _ = self._notifier(tmp_path)
        send_message = Mock()
        monkeypatch.setattr(fa.TelegramClient, "send_message", send_message)

        result = notifier.notify([_row("l1")], notify_all=True)

        assert result == 1
        send_message.assert_called_once()

    def test_only_sends_listings_not_seen_before(self, tmp_path, monkeypatch):
        notifier, config_store, listings_store, _ = self._notifier(tmp_path)
        listings_store.save([_row("l1")])
        config_store.update(results_initialized=True)
        send_message = Mock()
        monkeypatch.setattr(fa.TelegramClient, "send_message", send_message)

        result = notifier.notify([_row("l1"), _row("l2")])

        assert result == 1
        send_message.assert_called_once()
        assert listings_store.seen_links(["l1", "l2"]) == {"l1", "l2"}

    def test_uses_media_group_for_multiple_images(self, tmp_path, monkeypatch):
        notifier, config_store, _, _ = self._notifier(tmp_path)
        config_store.update(results_initialized=True)
        send_media_group = Mock()
        monkeypatch.setattr(fa.TelegramClient, "send_media_group", send_media_group)

        notifier.notify([_row("l1", images=["a.jpg", "b.jpg"])])

        send_media_group.assert_called_once()

    def test_uses_send_photo_for_single_image(self, tmp_path, monkeypatch):
        notifier, config_store, _, _ = self._notifier(tmp_path)
        config_store.update(results_initialized=True)
        send_photo = Mock()
        monkeypatch.setattr(fa.TelegramClient, "send_photo", send_photo)

        notifier.notify([_row("l1", images=["a.jpg"])])

        send_photo.assert_called_once()

    def test_truncates_caption_for_long_description_to_avoid_telegram_400(self, tmp_path, monkeypatch):
        notifier, config_store, _, _ = self._notifier(tmp_path)
        config_store.update(results_initialized=True)
        send_photo = Mock()
        monkeypatch.setattr(fa.TelegramClient, "send_photo", send_photo)

        row = _row("l1", images=["a.jpg"])
        row["description"] = "x" * 2000

        notifier.notify([row])

        sent_caption = send_photo.call_args.args[1]
        assert len(sent_caption) <= fa.TELEGRAM_CAPTION_LIMIT

    def test_uses_no_photo_file_when_no_images_and_file_exists(self, tmp_path, monkeypatch):
        notifier, config_store, _, no_photo_path = self._notifier(tmp_path)
        config_store.update(results_initialized=True)
        no_photo_path.write_bytes(b"fake")
        send_photo_file = Mock()
        monkeypatch.setattr(fa.TelegramClient, "send_photo_file", send_photo_file)

        notifier.notify([_row("l1", images=[])])

        send_photo_file.assert_called_once()

    def test_uses_text_message_when_no_images_and_no_fallback_photo(self, tmp_path, monkeypatch):
        notifier, config_store, _, _ = self._notifier(tmp_path)
        config_store.update(results_initialized=True)
        send_message = Mock()
        monkeypatch.setattr(fa.TelegramClient, "send_message", send_message)

        notifier.notify([_row("l1", images=[])])

        send_message.assert_called_once()

    def test_falls_back_to_text_message_when_send_fails(self, tmp_path, monkeypatch):
        notifier, config_store, _, _ = self._notifier(tmp_path)
        config_store.update(results_initialized=True)
        monkeypatch.setattr(fa.TelegramClient, "send_photo", Mock(side_effect=RuntimeError("boom")))
        send_message = Mock()
        monkeypatch.setattr(fa.TelegramClient, "send_message", send_message)

        result = notifier.notify([_row("l1", images=["a.jpg"])])

        assert result == 1
        send_message.assert_called_once()

    def test_counts_zero_when_both_send_and_fallback_fail(self, tmp_path, monkeypatch):
        notifier, config_store, _, _ = self._notifier(tmp_path)
        config_store.update(results_initialized=True)
        monkeypatch.setattr(fa.TelegramClient, "send_photo", Mock(side_effect=RuntimeError("boom")))
        monkeypatch.setattr(fa.TelegramClient, "send_message", Mock(side_effect=RuntimeError("boom again")))

        result = notifier.notify([_row("l1", images=["a.jpg"])])

        assert result == 0


# --------------------------------------------------------------------------
# KufarScraper
# --------------------------------------------------------------------------


class TestKufarScraper:
    def _payload(self):
        return {
            "ads": [
                {
                    "ad_id": 111,
                    "ad_parameters": [
                        {"p": "rooms", "v": "2"},
                        {"p": "size", "v": "45"},
                        {"p": "floor", "v": ["3"]},
                        {"p": "re_number_floors", "v": ["9"]},
                        {"p": "address", "v": "Независимости пр-т, 10"},
                        {"p": "coordinates", "v": [27.5590, 53.9006]},
                    ],
                    "subject": "2-комн. квартира",
                    "price_byn": 80000,
                    "ad_link": "https://kufar.by/item/111",
                    "list_time": "2026-07-20T10:00:00",
                    "images": [{"path": "abc.jpg"}],
                    "body_short": "<p>Хорошая квартира</p>",
                }
            ]
        }

    def test_fetch_parses_and_dedupes_across_area_params(self, monkeypatch):
        monkeypatch.setattr(fa, "http_get", lambda url: json.dumps(self._payload()).encode())

        rows = fa.KufarScraper().fetch(500, 1300)

        assert len(rows) == 1  # same ad_id returned for both AREA_PARAMS, deduped
        row = rows[0]
        assert row["source"] == "kufar.by"
        assert row["price_byn"] == 800.0
        assert row["rooms"] == "2"
        assert row["area_m2"] == "45"
        assert row["floor"] == "3"
        assert row["floors_total"] == "9"
        assert row["address"] == "Независимости пр-т, 10"
        assert row["updated"] == "2026-07-20"
        assert row["images"] == ["https://rms.kufar.by/v1/list_thumbs_2x/abc.jpg"]
        assert row["description"] == "Хорошая квартира"
        assert row["latitude"] == 53.9006
        assert row["longitude"] == 27.559
        assert row["_kufar_ad_id"] == 111

    def test_fetch_leaves_coordinates_none_when_missing(self, monkeypatch):
        payload = self._payload()
        payload["ads"][0]["ad_parameters"] = [p for p in payload["ads"][0]["ad_parameters"] if p["p"] != "coordinates"]
        monkeypatch.setattr(fa, "http_get", lambda url: json.dumps(payload).encode())

        rows = fa.KufarScraper().fetch(500, 1300)

        assert rows[0]["latitude"] is None
        assert rows[0]["longitude"] is None

    def test_fetch_falls_back_to_region_labels_when_no_address(self, monkeypatch):
        payload = self._payload()
        del payload["ads"][0]["ad_parameters"][4]  # drop explicit "address" param
        payload["ads"][0]["subject"] = "Аренда"  # generic title, no street pattern
        payload["ads"][0]["ad_parameters"].append({"p": "region", "v": "x", "vl": "Минск"})
        payload["ads"][0]["ad_parameters"].append({"p": "area", "v": "x", "vl": "Ленинский"})
        monkeypatch.setattr(fa, "http_get", lambda url: json.dumps(payload).encode())

        rows = fa.KufarScraper().fetch(500, 1300)

        assert rows[0]["address"] == "Минск, Ленинский р-н"

    def test_fetch_full_description_extracts_body_from_detail_page(self, monkeypatch):
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps({"props": {"initialState": {"adView": {"data": {"body": "<p>Полное описание</p>"}}}}})
            + "</script>"
        )
        monkeypatch.setattr(fa, "http_get", lambda url: html.encode())

        result = fa.KufarScraper().fetch_full_description(111)

        assert result == "Полное описание"

    def test_fetch_full_description_returns_none_without_next_data(self, monkeypatch):
        monkeypatch.setattr(fa, "http_get", lambda url: b"<html></html>")
        assert fa.KufarScraper().fetch_full_description(111) is None


# --------------------------------------------------------------------------
# RealtScraper
# --------------------------------------------------------------------------


class TestRealtScraper:
    def _html(self, objects, total_count, page_size=30):
        data = {
            "props": {
                "pageProps": {
                    "objects": objects,
                    "pagination": {"totalCount": total_count, "pageSize": page_size},
                }
            }
        }
        return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + "</script>"

    def test_fetch_parses_and_filters_by_price(self, monkeypatch):
        objects = [
            {
                "code": "abc",
                "priceRates": {"933": 900.0},
                "rooms": 2,
                "areaTotal": 45,
                "storey": 3,
                "storeys": 9,
                "address": "Немига ул., 5",
                "updatedAt": "2026-07-15T00:00:00",
                "images": ["img.jpg"],
                "description": "<b>Описание</b>",
                "location": [27.5590, 53.9006],
            },
            {
                "code": "def",
                "priceRates": {"933": 5000.0},
                "rooms": 2,
                "areaTotal": 45,
                "storey": 1,
                "storeys": 5,
                "address": "Другая ул., 1",
            },
        ]
        html = self._html(objects, total_count=1)
        monkeypatch.setattr(fa, "http_get", lambda url: html.encode("utf-8"))

        rows = fa.RealtScraper().fetch(500, 1300)

        assert len(rows) == 1  # out-of-range listing filtered out
        row = rows[0]
        assert row["source"] == "realt.by"
        assert row["price_byn"] == 900.0
        assert row["address"] == "Немига ул., 5"
        assert row["updated"] == "2026-07-15"
        assert row["description"] == "Описание"
        assert row["link"] == "https://realt.by/rent-flat-for-long/object/abc/"
        assert row["latitude"] == 53.9006
        assert row["longitude"] == 27.559

    def test_fetch_follows_pagination(self, monkeypatch):
        page1 = self._html([{"code": "p1", "priceRates": {"933": 900.0}}], total_count=2, page_size=1)
        page2 = self._html([{"code": "p2", "priceRates": {"933": 950.0}}], total_count=2, page_size=1)

        def fake_http_get(url):
            return (page2 if "page=2" in url else page1).encode("utf-8")

        monkeypatch.setattr(fa, "http_get", fake_http_get)

        rows = fa.RealtScraper().fetch(500, 1300)

        links = {row["link"] for row in rows}
        assert "https://realt.by/rent-flat-for-long/object/p1/" in links
        assert "https://realt.by/rent-flat-for-long/object/p2/" in links


# --------------------------------------------------------------------------
# ApartmentFinder (orchestration)
# --------------------------------------------------------------------------


class FakeScraper:
    def __init__(self, source_name, rows=None, error=None):
        self.source_name = source_name
        self._rows = rows or []
        self._error = error

    def fetch(self, price_min, price_max):
        if self._error:
            raise self._error
        return [dict(r) for r in self._rows]


def _default_args(**overrides):
    args = Namespace(min=500, max=1300, rooms="", notify_all=False)
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


class TestApartmentFinder:
    def _finder(self, kufar_rows=None, realt_rows=None, kufar_error=None, realt_error=None):
        # Bypass ApartmentFinder.__init__ entirely: it eagerly constructs a real
        # ListingsStore() (opens/creates listings.db at the default project-root
        # path), which run() below never needs since notifier is mocked anyway.
        finder = fa.ApartmentFinder.__new__(fa.ApartmentFinder)
        finder.kufar = FakeScraper("kufar.by", kufar_rows, kufar_error)
        finder.realt = FakeScraper("realt.by", realt_rows, realt_error)
        finder.scrapers = [finder.kufar, finder.realt]
        finder.deduper = fa.ListingDeduper()
        finder.notifier = Mock(notify=Mock(return_value=0))
        finder.listings_store = Mock(all_listings=Mock(return_value=[]), stored_descriptions=Mock(return_value={}))
        return finder

    def test_run_merges_sorts_by_price_and_saves(self, capsys):
        finder = self._finder(
            kufar_rows=[_row("k1", source="kufar.by")],
            realt_rows=[dict(_row("r1", source="realt.by", address="Немига ул., 5"), price_byn=100.0)],
        )
        finder.run(_default_args())

        notified_rows = finder.notifier.notify.call_args[0][0]
        assert [r["link"] for r in notified_rows] == ["r1", "k1"]  # cheaper first
        finder.notifier.notify.assert_called_once()
        assert "Сохранено 2 объявлений" in capsys.readouterr().out

    def test_run_filters_by_allowed_rooms(self):
        finder = self._finder(
            kufar_rows=[dict(_row("k1"), rooms="1"), dict(_row("k2"), rooms="2")],
        )
        finder.run(_default_args(rooms="2"))

        notified_rows = finder.notifier.notify.call_args[0][0]
        assert [r["link"] for r in notified_rows] == ["k2"]

    def test_run_collects_and_reports_scraper_errors(self, capsys):
        finder = self._finder(
            kufar_rows=[_row("k1")],
            realt_error=RuntimeError("realt.by is down"),
        )
        finder.run(_default_args())

        notified_rows = finder.notifier.notify.call_args[0][0]
        assert [r["link"] for r in notified_rows] == ["k1"]
        assert "realt.by is down" in capsys.readouterr().err

    def test_run_fills_in_full_kufar_description(self):
        finder = self._finder(
            kufar_rows=[dict(_row("k1"), description="short", _kufar_ad_id=111)],
        )
        finder.kufar.fetch_full_description = Mock(return_value="полное описание")

        finder.run(_default_args())

        notified_rows = finder.notifier.notify.call_args[0][0]
        assert notified_rows[0]["description"] == "полное описание"

    def test_run_reuses_stored_description_instead_of_refetching(self):
        finder = self._finder(
            kufar_rows=[dict(_row("k1"), description="short", _kufar_ad_id=111)],
        )
        finder.listings_store.stored_descriptions = Mock(return_value={"k1": "уже сохранённое описание"})
        finder.kufar.fetch_full_description = Mock(return_value="свежее описание")

        finder.run(_default_args())

        finder.kufar.fetch_full_description.assert_not_called()
        notified_rows = finder.notifier.notify.call_args[0][0]
        assert notified_rows[0]["description"] == "уже сохранённое описание"

    def test_retry_delay_honors_retry_after_header_on_429(self):
        headers = Message()
        headers["Retry-After"] = "7"
        error = urllib.error.HTTPError("url", 429, "Too Many Requests", headers, None)
        assert fa.ApartmentFinder._retry_delay(error, attempt=0) == 7.0

    def test_retry_delay_backs_off_progressively_on_429_without_retry_after(self):
        error = urllib.error.HTTPError("url", 429, "Too Many Requests", Message(), None)
        assert fa.ApartmentFinder._retry_delay(error, attempt=0) == 5.0
        assert fa.ApartmentFinder._retry_delay(error, attempt=1) == 10.0

    def test_retry_delay_is_flat_one_second_for_non_429_errors(self):
        assert fa.ApartmentFinder._retry_delay(RuntimeError("boom"), attempt=0) == 1.0

    def test_run_prints_a_table_of_all_stored_listings(self, tmp_path, capsys):
        # notifier is mocked (see _finder), so it never touches listings_store itself -
        # the table below reflects only what's seeded here, independent of this run's rows.
        finder = self._finder(kufar_rows=[_row("k1")])
        finder.listings_store = fa.ListingsStore(tmp_path / "listings.db")
        finder.listings_store.save([dict(_row("old", address="Немига ул., 5"), price_byn=900.0)])

        finder.run(_default_args())

        out = capsys.readouterr().out
        assert "Все объявления в базе (1)" in out
        assert "Немига ул., 5" in out
        assert "900" in out
