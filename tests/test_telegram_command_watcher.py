from unittest.mock import Mock

import pytest

import telegram_command_watcher as tcw


class TestCommandWatcher:
    def test_run_processes_updates_advances_offset_and_saves_on_change(self, monkeypatch):
        client = Mock(token="TOKEN")
        settings_store = Mock()
        settings_store.load.side_effect = [
            {"min_price": 500, "max_price": 1300, "rooms": "2,3"},
            RuntimeError("stop the loop"),
        ]
        offset_store = Mock()
        offset_store.load.return_value = 10
        processor = Mock()
        processor.process.return_value = True

        resp = Mock(raise_for_status=Mock())
        resp.json.return_value = {"result": [{"update_id": 10, "message": {"text": "цена 1 2"}}]}
        get = Mock(return_value=resp)
        monkeypatch.setattr(tcw.requests, "get", get)

        watcher = tcw.CommandWatcher(client, settings_store, offset_store, processor)
        with pytest.raises(RuntimeError, match="stop the loop"):
            watcher.run()

        # first poll uses the offset loaded at startup; a second poll follows
        # (with the advanced offset) before settings_store.load() raises and stops the loop
        get.assert_any_call(
            "https://api.telegram.org/botTOKEN/getUpdates",
            params={"offset": 10, "timeout": 25},
            timeout=35,
        )
        offset_store.save.assert_called_once_with(11)
        settings_store.save.assert_called_once()

    def test_run_skips_processing_when_no_updates(self, monkeypatch):
        # An empty poll must "continue" straight back to polling, without touching
        # settings/processor. To observe that and still let the test terminate, the
        # second poll raises - caught by the loop's own poll-error handler - and then
        # time.sleep() (called from that handler) is what actually stops the loop.
        client = Mock(token="TOKEN")
        settings_store = Mock()
        offset_store = Mock()
        offset_store.load.return_value = 0
        processor = Mock()

        empty_resp = Mock(raise_for_status=Mock())
        empty_resp.json.return_value = {"result": []}
        monkeypatch.setattr(
            tcw.requests,
            "get",
            Mock(side_effect=[empty_resp, RuntimeError("network down")]),
        )
        monkeypatch.setattr(tcw.time, "sleep", Mock(side_effect=RuntimeError("stop after empty poll")))

        watcher = tcw.CommandWatcher(client, settings_store, offset_store, processor)
        with pytest.raises(RuntimeError, match="stop after empty poll"):
            watcher.run()

        settings_store.load.assert_not_called()
        processor.process.assert_not_called()

    def test_run_handles_poll_errors_without_crashing(self, monkeypatch, capsys):
        client = Mock(token="TOKEN")
        settings_store = Mock()
        offset_store = Mock()
        offset_store.load.return_value = 0
        processor = Mock()

        monkeypatch.setattr(tcw.requests, "get", Mock(side_effect=RuntimeError("network down")))
        monkeypatch.setattr(tcw.time, "sleep", Mock(side_effect=RuntimeError("stop after sleep")))

        watcher = tcw.CommandWatcher(client, settings_store, offset_store, processor)
        with pytest.raises(RuntimeError, match="stop after sleep"):
            watcher.run()

        assert "Ошибка опроса: network down" in capsys.readouterr().out
        processor.process.assert_not_called()
