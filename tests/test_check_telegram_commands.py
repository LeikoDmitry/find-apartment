from unittest.mock import Mock

import check_telegram_commands as ctc
import find_apartments as fa

# --------------------------------------------------------------------------
# OffsetStore
# --------------------------------------------------------------------------


class TestOffsetStore:
    def test_load_returns_zero_when_file_missing(self, tmp_path):
        store = ctc.OffsetStore(fa.ConfigStore(tmp_path / "missing.yaml"))
        assert store.load() == 0

    def test_save_and_load_roundtrip(self, tmp_path):
        config_store = fa.ConfigStore(tmp_path / "config.yaml")
        store = ctc.OffsetStore(config_store)
        store.save(42)
        assert store.load() == 42

    def test_save_does_not_clobber_other_config_keys(self, tmp_path):
        config_store = fa.ConfigStore(tmp_path / "config.yaml")
        config_store.update(min_price=600)
        store = ctc.OffsetStore(config_store)

        store.save(7)

        assert config_store.load()["min_price"] == 600


# --------------------------------------------------------------------------
# CommandProcessor
# --------------------------------------------------------------------------


class TestCommandProcessor:
    def _processor(self):
        client = Mock()
        return ctc.CommandProcessor(client), client

    def test_ignores_message_without_text(self):
        processor, client = self._processor()
        settings = {"min_price": 500, "max_price": 1300, "rooms": "2,3"}
        assert processor.process({"message": {}}, settings) is False
        client.send_message.assert_not_called()

    def test_ignores_update_without_message(self):
        processor, client = self._processor()
        settings = {"min_price": 500, "max_price": 1300, "rooms": "2,3"}
        assert processor.process({}, settings) is False
        client.send_message.assert_not_called()

    def test_ignores_unrecognized_text(self):
        processor, client = self._processor()
        settings = {"min_price": 500, "max_price": 1300, "rooms": "2,3"}
        assert processor.process({"message": {"text": "привет"}}, settings) is False
        client.send_message.assert_not_called()

    def test_price_command_updates_settings_and_acknowledges(self):
        processor, client = self._processor()
        settings = {"min_price": 500, "max_price": 1300, "rooms": "2,3"}

        changed = processor.process({"message": {"text": "цена 600 1200"}}, settings)

        assert changed is True
        assert settings["min_price"] == 600
        assert settings["max_price"] == 1200
        client.send_message.assert_called_once()
        ack_text = client.send_message.call_args[0][0]
        assert "600" in ack_text and "1200" in ack_text
        assert "10 минут" in ack_text

    def test_price_command_sorts_reversed_bounds(self):
        processor, client = self._processor()
        settings = {"min_price": 500, "max_price": 1300, "rooms": "2,3"}

        processor.process({"message": {"text": "цена 1200 600"}}, settings)

        assert settings["min_price"] == 600
        assert settings["max_price"] == 1200

    def test_price_command_is_case_insensitive(self):
        processor, client = self._processor()
        settings = {"min_price": 500, "max_price": 1300, "rooms": "2,3"}

        changed = processor.process({"message": {"text": "ЦЕНА 100 200"}}, settings)

        assert changed is True

    def test_rooms_command_dedupes_and_sorts(self):
        processor, client = self._processor()
        settings = {"min_price": 500, "max_price": 1300, "rooms": "2,3"}

        changed = processor.process({"message": {"text": "комнаты 3,1,2, 2"}}, settings)

        assert changed is True
        assert settings["rooms"] == "1,2,3"
        client.send_message.assert_called_once()
        assert "1,2,3" in client.send_message.call_args[0][0]


# --------------------------------------------------------------------------
# main()
# --------------------------------------------------------------------------


class TestMain:
    def test_prints_message_when_telegram_not_configured(self, monkeypatch, capsys):
        monkeypatch.setattr(ctc, "TelegramClient", Mock(from_config=Mock(side_effect=fa.TelegramNotConfigured)))

        ctc.main()

        assert "Telegram не настроен" in capsys.readouterr().out

    def test_prints_message_when_no_new_updates(self, monkeypatch, capsys):
        client = Mock(token="TOKEN")
        monkeypatch.setattr(ctc, "TelegramClient", Mock(from_config=Mock(return_value=client)))
        offset_store = Mock(load=Mock(return_value=0), save=Mock())
        monkeypatch.setattr(ctc, "OffsetStore", Mock(return_value=offset_store))

        resp = Mock(raise_for_status=Mock())
        resp.json.return_value = {"result": []}
        monkeypatch.setattr(ctc.requests, "get", Mock(return_value=resp))

        ctc.main()

        assert "Новых сообщений нет." in capsys.readouterr().out
        offset_store.save.assert_not_called()

    def test_processes_recognized_command_and_persists_state(self, monkeypatch, capsys):
        client = Mock(token="TOKEN")
        monkeypatch.setattr(ctc, "TelegramClient", Mock(from_config=Mock(return_value=client)))

        offset_store = Mock(load=Mock(return_value=5), save=Mock())
        monkeypatch.setattr(ctc, "OffsetStore", Mock(return_value=offset_store))

        settings_store = Mock(
            load=Mock(return_value={"min_price": 500, "max_price": 1300, "rooms": "2,3"}),
            save=Mock(),
        )
        monkeypatch.setattr(ctc, "SettingsStore", Mock(return_value=settings_store))

        resp = Mock(raise_for_status=Mock())
        resp.json.return_value = {"result": [{"update_id": 6, "message": {"text": "цена 600 1200"}}]}
        monkeypatch.setattr(ctc.requests, "get", Mock(return_value=resp))

        ctc.main()

        settings_store.save.assert_called_once()
        offset_store.save.assert_called_once_with(7)
        assert "Настройки обновлены" in capsys.readouterr().out

    def test_no_recognized_commands_still_advances_offset(self, monkeypatch, capsys):
        client = Mock(token="TOKEN")
        monkeypatch.setattr(ctc, "TelegramClient", Mock(from_config=Mock(return_value=client)))

        offset_store = Mock(load=Mock(return_value=5), save=Mock())
        monkeypatch.setattr(ctc, "OffsetStore", Mock(return_value=offset_store))

        settings_store = Mock(
            load=Mock(return_value={"min_price": 500, "max_price": 1300, "rooms": "2,3"}),
            save=Mock(),
        )
        monkeypatch.setattr(ctc, "SettingsStore", Mock(return_value=settings_store))

        resp = Mock(raise_for_status=Mock())
        resp.json.return_value = {"result": [{"update_id": 6, "message": {"text": "привет"}}]}
        monkeypatch.setattr(ctc.requests, "get", Mock(return_value=resp))

        ctc.main()

        settings_store.save.assert_not_called()
        offset_store.save.assert_called_once_with(7)
        assert "Распознанных команд" in capsys.readouterr().out
